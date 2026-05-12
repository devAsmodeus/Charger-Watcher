from __future__ import annotations

import asyncio
import contextlib
import html
import signal
from datetime import UTC, datetime, time, timedelta

import orjson
import redis.asyncio as aioredis
import structlog
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from bot.geo import find_nearby
from bot.notifier import Notifier, stale_claim_reaper, tier_reaper
from bot.onboarding import (
    BTN_FIND,
    BTN_LIST,
    BTN_REFERRAL,
    BTN_SETTINGS,
    BTN_TIER,
    GREETING_NEW,
    GREETING_RETURNING,
    about_kb,
    about_text,
    main_reply_kb,
    upgrade_only_kb,
)
from config import get_settings
from db.models import (
    Location,
    NotificationLog,
    Payment,
    Referral,
    Subscription,
    SubscriptionMode,
    Tier,
    User,
)
from db.session import SessionLocal
from logging_setup import setup_logging

log = structlog.get_logger(__name__)
dp = Dispatcher()

# Пауза между карточками локаций в /find и /nearby. Telegram гасит
# бёрсты быстрее ~5 msg/s в один чат — десяток подряд почти всегда
# словит FloodWait и сообщения уйдут с задержкой / не уйдут вовсе.
LIST_THROTTLE_SEC = 0.35

# Sync с poller-ом: hash, заполняемый periodic-таской connectors_catalog_tick.
LOCATION_CONNECTORS_HASH = "location_connectors"

# Опции лимита уведомлений в wizard'е. 0 — кодирует "∞" в callback_data
# (NULL в БД). Telegram callback_data ограничен 64 байтами, длинные
# строки сюда не помещаются.
LIMIT_OPTIONS: list[tuple[str, int]] = [
    ("1 уведомление", 1),
    ("2 уведомления", 2),
    ("3 уведомления", 3),
    ("5 уведомлений", 5),
    ("10 уведомлений", 10),
    ("∞ всегда", 0),
]

# Глобальный handle на Redis — handlers не получают его параметром, а
# DI-middleware aiogram'а здесь избыточен. Инициализируется в _runner.
_redis_instance: aioredis.Redis | None = None


def _redis() -> aioredis.Redis:
    if _redis_instance is None:
        raise RuntimeError("Redis is not initialized — _runner did not start")
    return _redis_instance


# ---------- helpers ----------

async def ensure_user(tg_id: int) -> User:
    async with SessionLocal() as s:
        stmt = (
            pg_insert(User)
            .values(tg_id=tg_id, tier=Tier.FREE.value)
            .on_conflict_do_nothing(index_elements=["tg_id"])
        )
        await s.execute(stmt)
        await s.commit()
        user = await s.get(User, tg_id)
    assert user is not None
    return user


async def ensure_user_with_flag(tg_id: int) -> tuple[User, bool]:
    """Like ensure_user but also returns True iff this is a brand-new row."""
    async with SessionLocal() as s:
        existing = await s.get(User, tg_id)
        if existing is not None:
            return existing, False
        stmt = (
            pg_insert(User)
            .values(tg_id=tg_id, tier=Tier.FREE.value)
            .on_conflict_do_nothing(index_elements=["tg_id"])
        )
        await s.execute(stmt)
        await s.commit()
        user = await s.get(User, tg_id)
    assert user is not None
    return user, True


def _effective_tier(user: User) -> str:
    """Treat expired paid users as free without touching the DB."""
    if (
        user.tier == Tier.PAID.value
        and user.paid_until
        and user.paid_until < datetime.now(UTC)
    ):
        return Tier.FREE.value
    return user.tier


def tier_limit(tier: str) -> int:
    s = get_settings()
    return s.paid_tier_max_subscriptions if tier == Tier.PAID.value else s.free_tier_max_subscriptions


async def user_subscription_count(tg_id: int) -> int:
    async with SessionLocal() as s:
        result = await s.execute(
            select(Subscription).where(Subscription.user_tg_id == tg_id)
        )
        return len(result.scalars().all())


def _status_icon(status: str | None) -> str:
    return {"AVAILABLE": "🟢", "FULLY_USED": "🔴", "UNAVAILABLE": "⚪"}.get(status or "", "❔")


def _subscribe_kb(location_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔔 Подписаться", callback_data=f"sub:{location_id}")]
        ]
    )


# ---------- subscribe wizard helpers ----------

# Cap на число типов коннекторов в keyboard'е — реалистичный потолок
# на станции = 3-4, девятку оставляем как защиту от мусорных данных.
_CONNECTOR_KB_CAP = 9


async def _connector_types_for(location_id: int) -> list[str]:
    """Читает Redis-кэш, наполняемый poller-ом (connectors_catalog_tick).

    Пустой результат = poller ещё не успел синхронизировать (boot) или
    upstream API не отдаёт detail для этой станции — wizard fallback'ит
    на «любой коннектор» одной кнопкой.
    """
    raw = await _redis().hget(LOCATION_CONNECTORS_HASH, str(location_id))
    if not raw:
        return []
    try:
        types = orjson.loads(raw)
    except Exception:
        return []
    if not isinstance(types, list):
        return []
    return [t for t in types if isinstance(t, str)][:_CONNECTOR_KB_CAP]


def _connector_kb(location_id: int, types: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, t in enumerate(types):
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🔌 {t}", callback_data=f"wcon:{location_id}:{i}"
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔌 Любой коннектор", callback_data=f"wcon:{location_id}:a"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _limit_kb(location_id: int, con_token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"wlim:{location_id}:{con_token}:{n}",
                )
            ]
            for label, n in LIMIT_OPTIONS
        ]
    )


async def _resolve_connector_token(
    location_id: int, con_token: str
) -> str | None | bool:
    """Возвращает выбранный тип коннектора или None для «любой».

    False — токен невалиден (idx за пределами текущего кэша); вызывающий
    отвечает юзеру и выходит.
    """
    if con_token == "a":
        return None
    try:
        idx = int(con_token)
    except ValueError:
        return False
    types = await _connector_types_for(location_id)
    if idx < 0 or idx >= len(types):
        return False
    return types[idx]


# ---------- handlers ----------

async def _try_record_referral(invitee_tg: int, raw_payload: str) -> int | None:
    """Если payload похож на `ref_<inviter_tg>` и проходит анти-фрод —
    пишем строку в `referrals`. Возвращает tg_id инвайтера или None.

    Анти-фрод:
      - формат `ref_<digits>`;
      - inviter != invitee (CHECK на уровне БД, дублируем тут для UX);
      - inviter существует в users (иначе ссылка бесполезна);
      - у инвайти ещё нет записи в referrals (PK invitee_tg_id).
    """
    if not raw_payload.startswith("ref_"):
        return None
    rest = raw_payload[4:]
    if not rest.isdigit():
        return None
    inviter_tg = int(rest)
    if inviter_tg == invitee_tg:
        return None
    async with SessionLocal() as s:
        inviter = await s.get(User, inviter_tg)
        if inviter is None:
            return None
        stmt = (
            pg_insert(Referral)
            .values(invitee_tg_id=invitee_tg, inviter_tg_id=inviter_tg)
            .on_conflict_do_nothing(index_elements=["invitee_tg_id"])
        )
        result = await s.execute(stmt)
        await s.commit()
        return inviter_tg if result.rowcount > 0 else None


@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject) -> None:
    if message.from_user is None:
        return
    _, is_new = await ensure_user_with_flag(message.from_user.id)
    raw = (command.args or "").strip()
    if raw:
        inviter_tg = await _try_record_referral(message.from_user.id, raw)
        if inviter_tg is not None:
            s = get_settings()
            log.info("referral_recorded", invitee=message.from_user.id, inviter=inviter_tg)
            # Юзеру сообщим о скидке на первый paid отдельным сообщением —
            # после greeting и инлайн-клавы. Чтобы не пропало.
            await message.answer(
                f"🎁 Ты пришёл по приглашению. Первый <b>paid</b> "
                f"со скидкой — <b>{s.referral_invitee_price_stars} ⭐</b> "
                f"вместо {s.paid_tier_price_stars} ⭐. /upgrade когда готов.",
                parse_mode="HTML",
            )
    greeting = GREETING_NEW if is_new else GREETING_RETURNING
    await message.answer(greeting, reply_markup=main_reply_kb())


@dp.message(Command("about"))
async def cmd_about(message: Message) -> None:
    await message.answer(about_text(), parse_mode="HTML", reply_markup=about_kb())


async def _send_privacy(message: Message) -> None:
    await message.answer(
        "<b>Политика конфиденциальности</b>\n\n"
        "Сервис хранит минимум данных:\n"
        "• Ваш Telegram ID (без имени/телефона)\n"
        "• Список ваших подписок (id локации)\n"
        "• Лог отправленных уведомлений (для дедупа)\n\n"
        "Данные не передаются третьим лицам и используются только для рассылки уведомлений. "
        "Удалить все свои данные можно через <b>/about → 🗑 Удалить данные</b> "
        "или командой <code>/delete_me</code> — действие необратимо.\n\n"
        "Сервис не является официальным от оператора зарядных станций. "
        "Вопросы и жалобы — через /about → контакты автора.",
        parse_mode="HTML",
    )


async def _send_delete_confirm(message: Message) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Да, удалить навсегда", callback_data="delme:y")],
            [InlineKeyboardButton(text="Отмена", callback_data="delme:n")],
        ]
    )
    await message.answer(
        "⚠️ Удалить все данные?\n\n"
        "• все подписки слетят\n"
        "• если у тебя сейчас paid — статус сгорит, оплату не вернуть\n"
        "• ещё не доставленные уведомления не придут\n\n"
        "Действие необратимо.",
        reply_markup=kb,
    )


@dp.message(Command("privacy"))
async def cmd_privacy(message: Message) -> None:
    await _send_privacy(message)


@dp.message(Command("delete_me"))
async def cmd_delete_me(message: Message) -> None:
    if message.from_user is None:
        return
    await _send_delete_confirm(message)


@dp.callback_query(F.data.startswith("delme:"))
async def on_delme_callback(cb: CallbackQuery) -> None:
    if cb.from_user is None or cb.data is None or cb.message is None:
        return
    action = cb.data.split(":", 1)[1]
    if action == "n":
        await cb.answer("Отменено")
        await cb.message.edit_text("Удаление отменено. Подписки на месте.")
        return
    if action != "y":
        await cb.answer()
        return
    tg_id = cb.from_user.id
    async with SessionLocal() as s:
        await s.execute(delete(NotificationLog).where(
            NotificationLog.subscription_id.in_(
                select(Subscription.id).where(Subscription.user_tg_id == tg_id)
            )
        ))
        await s.execute(delete(Subscription).where(Subscription.user_tg_id == tg_id))
        await s.execute(delete(User).where(User.tg_id == tg_id))
        await s.commit()
    await cb.answer("Удалено")
    await cb.message.edit_text("Все данные удалены. /start — начать заново.")


async def _send_nearby_prompt(message: Message) -> None:
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer("Пришли геолокацию — найду 10 ближайших станций.", reply_markup=kb)


@dp.message(Command("nearby"))
async def cmd_nearby(message: Message) -> None:
    await _send_nearby_prompt(message)


@dp.message(F.location)
async def on_location(message: Message) -> None:
    if message.from_user is None or message.location is None:
        return
    await ensure_user(message.from_user.id)
    lat = message.location.latitude
    lon = message.location.longitude
    async with SessionLocal() as s:
        hits = await find_nearby(s, lat, lon, radius_km=5.0, limit=10)
    if not hits:
        await message.answer("В радиусе 5 км ничего не нашёл.", reply_markup=ReplyKeyboardRemove())
        return
    await message.answer(
        f"Нашёл {len(hits)} станций в радиусе 5 км:", reply_markup=ReplyKeyboardRemove()
    )
    for i, h in enumerate(hits):
        loc = h.location
        text = (
            f"{_status_icon(loc.last_status)} <b>{html.escape(loc.name)}</b>\n"
            f"{html.escape(loc.address)}\n"
            f"📏 {h.distance_km:.2f} км · сеть: {html.escape(loc.operator)}"
        )
        await message.answer(text, parse_mode="HTML", reply_markup=_subscribe_kb(loc.id))
        # Telegram per-chat бёрстит ~5 msg/s до FloodWait. 10 карточек подряд
        # уверенно его триггерят — раздаём с интервалом, кроме последней.
        if i < len(hits) - 1:
            await asyncio.sleep(LIST_THROTTLE_SEC)


@dp.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject) -> None:
    q = (command.args or "").strip().lower()
    if not q:
        await message.answer("Укажи фрагмент адреса или названия. Пример: /find Минск Пулихова")
        return
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(Location)
                .where(
                    (Location.name.ilike(f"%{q}%")) | (Location.address.ilike(f"%{q}%"))
                )
                .limit(10)
            )
        ).scalars().all()
    if not rows:
        await message.answer("Ничего не нашёл. Попробуй другой запрос.")
        return
    for i, loc in enumerate(rows):
        text = (
            f"{_status_icon(loc.last_status)} <b>{html.escape(loc.name)}</b>\n"
            f"{html.escape(loc.address)}\n"
            f"сеть: {html.escape(loc.operator)}"
        )
        await message.answer(text, parse_mode="HTML", reply_markup=_subscribe_kb(loc.id))
        if i < len(rows) - 1:
            await asyncio.sleep(LIST_THROTTLE_SEC)


async def _send_list(message: Message, tg_id: int) -> None:
    async with SessionLocal() as s:
        rows = (
            await s.execute(
                select(Subscription, Location)
                .join(Location, Subscription.location_id == Location.id, isouter=True)
                .where(Subscription.user_tg_id == tg_id)
            )
        ).all()
    if not rows:
        await message.answer("У тебя пока нет подписок. /nearby или /find + 🔔.")
        return
    lines = [
        f"• {(loc.name if loc else 'geo')} — {(loc.last_status if loc and loc.last_status else 'статус неизвестен')}"
        for sub, loc in rows
    ]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"❌ Снять «{(loc.name if loc else 'geo')[:40]}»",
                    callback_data=f"unsub:{sub.id}",
                )
            ]
            for sub, loc in rows
        ]
    )
    await message.answer("Твои подписки:\n" + "\n".join(lines), reply_markup=kb)


@dp.message(Command("list"))
async def cmd_list(message: Message) -> None:
    if message.from_user is None:
        return
    await _send_list(message, message.from_user.id)


@dp.callback_query(F.data.startswith("sub:"))
async def on_sub_callback(cb: CallbackQuery) -> None:
    """Wizard step 1: показать клавиатуру выбора типа коннектора.

    Состояние wizard'а целиком закодировано в callback_data следующих
    кнопок (location_id + connector idx + лимит) — отдельный Redis-state
    не нужен, переживает рестарт бота.
    """
    if cb.from_user is None or cb.data is None or cb.message is None:
        return
    try:
        location_id = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer("Плохой id")
        return
    user = await ensure_user(cb.from_user.id)
    tier = _effective_tier(user)
    count = await user_subscription_count(user.tg_id)
    if count >= tier_limit(tier):
        await cb.answer(
            f"Лимит подписок ({tier_limit(tier)}). Сними одну в /list или /upgrade.",
            show_alert=True,
        )
        return
    async with SessionLocal() as s:
        loc = await s.get(Location, location_id)
    if loc is None:
        await cb.answer("Локация не найдена")
        return
    types = await _connector_types_for(location_id)
    await cb.answer()
    if not types:
        # Кэш ещё не наполнился poller-ом или станция без detail —
        # сразу к шагу лимита, тип = «любой».
        await cb.message.answer(
            f"Подписка на: {loc.name}\nКоннектор: любой.\nСколько уведомлений хочешь?",
            reply_markup=_limit_kb(location_id, "a"),
        )
        return
    await cb.message.answer(
        f"Подписка на: {loc.name}\nВыбери тип коннектора:",
        reply_markup=_connector_kb(location_id, types),
    )


@dp.callback_query(F.data.startswith("wcon:"))
async def on_wcon_callback(cb: CallbackQuery) -> None:
    """Wizard step 2: после выбора коннектора — keyboard выбора лимита."""
    if cb.from_user is None or cb.data is None or cb.message is None:
        return
    parts = cb.data.split(":")
    if len(parts) != 3:
        await cb.answer("Плохой id")
        return
    try:
        location_id = int(parts[1])
    except ValueError:
        await cb.answer("Плохой id")
        return
    con_token = parts[2]
    resolved = await _resolve_connector_token(location_id, con_token)
    if resolved is False:
        await cb.answer("Тип коннектора недоступен — попробуй ещё раз.")
        return
    type_str = resolved if isinstance(resolved, str) else "любой"
    await cb.answer()
    await cb.message.edit_text(
        f"Коннектор: {type_str}.\nСколько уведомлений хочешь?",
        reply_markup=_limit_kb(location_id, con_token),
    )


@dp.callback_query(F.data.startswith("wlim:"))
async def on_wlim_callback(cb: CallbackQuery) -> None:
    """Wizard step 3: создаём подписку с выбранным connector_type/notify_limit."""
    if cb.from_user is None or cb.data is None or cb.message is None:
        return
    parts = cb.data.split(":")
    if len(parts) != 4:
        await cb.answer("Плохой id")
        return
    try:
        location_id = int(parts[1])
        n = int(parts[3])
    except ValueError:
        await cb.answer("Плохой id")
        return
    con_token = parts[2]
    resolved = await _resolve_connector_token(location_id, con_token)
    if resolved is False:
        await cb.answer("Тип коннектора недоступен — попробуй ещё раз.")
        return
    connector_type: str | None = resolved if isinstance(resolved, str) else None
    notify_limit: int | None = None if n == 0 else n

    user = await ensure_user(cb.from_user.id)
    # Re-check tier limit — между шагами 1 и 3 юзер мог подписаться
    # с другого устройства.
    tier = _effective_tier(user)
    count = await user_subscription_count(user.tg_id)
    if count >= tier_limit(tier):
        await cb.answer(
            f"Лимит подписок ({tier_limit(tier)}). Сними одну в /list или /upgrade.",
            show_alert=True,
        )
        return
    async with SessionLocal() as s:
        loc = await s.get(Location, location_id)
        if loc is None:
            await cb.answer("Локация не найдена")
            return
        sub = Subscription(
            user_tg_id=user.tg_id,
            mode=SubscriptionMode.LOCATION.value,
            location_id=location_id,
            connector_type=connector_type,
            notify_limit=notify_limit,
        )
        s.add(sub)
        try:
            await s.commit()
        except IntegrityError:
            # Двойной клик / гонка — partial unique index сработал.
            await s.rollback()
            await cb.answer("Уже подписан на эту локацию.")
            return
    limit_str = "∞ (всегда)" if notify_limit is None else f"{notify_limit} раз"
    type_str = connector_type or "любой"
    await cb.answer("Подписка оформлена ✔")
    await cb.message.edit_text(
        f"✅ Подписан: {loc.name}\nКоннектор: {type_str}\nЛимит уведомлений: {limit_str}"
    )


@dp.callback_query(F.data.startswith("unsub:"))
async def on_unsub_callback(cb: CallbackQuery) -> None:
    if cb.from_user is None or cb.data is None:
        return
    try:
        sub_id = int(cb.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await cb.answer("Плохой id")
        return
    async with SessionLocal() as s:
        sub = await s.get(Subscription, sub_id)
        if sub is None or sub.user_tg_id != cb.from_user.id:
            await cb.answer("Подписка не найдена")
            return
        await s.delete(sub)
        await s.commit()
    await cb.answer("Подписка удалена ✔", show_alert=False)


@dp.callback_query(F.data.startswith("onboard:"))
async def on_onboard_callback(cb: CallbackQuery) -> None:
    """Inline-кнопки из /about: апгрейд, политика, удаление данных.

    Callback `cb.message` — это бот-сообщение `/about`, на которое навешана
    клавиатура; reply через `.answer()` шлёт НОВОЕ сообщение в чат, не
    портя текст /about.
    """
    if cb.from_user is None or cb.data is None or cb.message is None:
        return
    action = cb.data.split(":", 1)[1]
    if action == "upgrade":
        await cb.answer()
        await _send_upgrade_invoice(cb.message)
        return
    if action == "privacy":
        await cb.answer()
        await _send_privacy(cb.message)
        return
    if action == "delete":
        await cb.answer()
        await _send_delete_confirm(cb.message)
        return
    await cb.answer("Неизвестное действие")


@dp.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message, command: CommandObject) -> None:
    if message.from_user is None:
        return
    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer("Укажи id подписки из /list. Пример: /unsubscribe 42")
        return
    sub_id = int(raw)
    async with SessionLocal() as s:
        sub = await s.get(Subscription, sub_id)
        if sub is None or sub.user_tg_id != message.from_user.id:
            await message.answer("Подписка не найдена.")
            return
        await s.delete(sub)
        await s.commit()
    await message.answer("Подписка удалена.")


async def _send_tier(message: Message, tg_id: int) -> None:
    """Карточка тарифа — общий код /status и кнопки `💎 Тариф`.

    HTML-вёрстка под стиль карточек /nearby и /find. Для free добавляется
    блок «как расширить» + inline-кнопка апгрейда; paid показывает,
    сколько дней осталось до истечения.
    """
    user = await ensure_user(tg_id)
    tier = _effective_tier(user)
    used = await user_subscription_count(tg_id)
    limit = tier_limit(tier)
    s = get_settings()

    head = "💎 <b>Тариф: paid</b>" if tier == Tier.PAID.value else "🆓 <b>Тариф: free</b>"

    rows = [head, ""]
    if tier == Tier.PAID.value and user.paid_until:
        now = datetime.now(UTC)
        delta = user.paid_until - now
        days_left = max(0, delta.days)
        rows.append(
            f"Действует до: <b>{user.paid_until:%d.%m.%Y, %H:%M}</b> UTC "
            f"(осталось {days_left} дн.)"
        )
    rows.append(f"Подписки: <b>{used} / {limit}</b>")
    if tier == Tier.FREE.value:
        rows.append(f"Задержка уведомлений: {s.free_tier_notify_delay_sec // 60} мин")
        rows.append("")
        rows.append(
            f"💎 На paid — до {s.paid_tier_max_subscriptions} подписок и "
            "мгновенные пуши без задержки."
        )
        rows.append(
            f"Стоимость: <b>{s.paid_tier_price_stars} ⭐</b> / "
            f"{s.paid_tier_duration_days} дней."
        )
    kb: InlineKeyboardMarkup | None = (
        upgrade_only_kb() if tier == Tier.FREE.value else None
    )
    await message.answer("\n".join(rows), parse_mode="HTML", reply_markup=kb)


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if message.from_user is None:
        return
    await _send_tier(message, message.from_user.id)


async def _has_unclaimed_referral(tg_id: int) -> bool:
    """True если у юзера есть запись в `referrals` с `invitee_charge_id IS NULL`.

    Используется в _send_upgrade_invoice и pre_checkout для решения о скидке.
    """
    async with SessionLocal() as s:
        ref = await s.get(Referral, tg_id)
    return ref is not None and ref.invitee_charge_id is None


async def _send_upgrade_invoice(message: Message) -> None:
    s = get_settings()
    user_id = message.from_user.id if message.from_user else 0
    is_ref = bool(user_id) and await _has_unclaimed_referral(user_id)
    if is_ref:
        amount = s.referral_invitee_price_stars
        payload = f"paid:{s.paid_tier_duration_days}:ref"
        description = (
            f"🎁 По приглашению: {amount} ⭐ вместо {s.paid_tier_price_stars} ⭐.\n"
            f"{s.paid_tier_duration_days} дней paid · до {s.paid_tier_max_subscriptions} подписок, "
            "уведомления без задержки."
        )
    else:
        amount = s.paid_tier_price_stars
        payload = f"paid:{s.paid_tier_duration_days}"
        description = (
            f"{s.paid_tier_duration_days} дней: до {s.paid_tier_max_subscriptions} подписок, "
            "мгновенные уведомления без задержки."
        )
    await message.answer_invoice(
        title="Charger Watcher — Paid",
        description=description,
        prices=[LabeledPrice(label="Подписка", amount=amount)],
        currency="XTR",
        payload=payload,
        provider_token="",
    )


@dp.message(Command("upgrade"))
async def cmd_upgrade(message: Message) -> None:
    if message.from_user is None:
        return
    await _send_upgrade_invoice(message)


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery) -> None:
    """Validate that the invoice the client is paying matches what we sent.

    Telegram's pre_checkout step is the only chance to reject a tampered
    invoice — once we answer ok=True, ``successful_payment`` arrives and
    ``on_paid`` will hand out PAID. We check (currency, amount, payload)
    against the canonical values from settings; any mismatch is rejected
    with a user-visible message and logged for audit.

    Поддерживаются два варианта payload:
      - `paid:<days>`     — стандартная цена
      - `paid:<days>:ref` — скидка для инвайти, только если у юзера есть
        unclaimed referral (invitee_charge_id IS NULL)
    """
    s = get_settings()
    is_ref = (q.invoice_payload or "").endswith(":ref")
    expected_payload = (
        f"paid:{s.paid_tier_duration_days}:ref"
        if is_ref
        else f"paid:{s.paid_tier_duration_days}"
    )
    expected_amount = (
        s.referral_invitee_price_stars if is_ref else s.paid_tier_price_stars
    )
    reason: str | None = None
    if q.currency != "XTR":
        reason = f"currency={q.currency}"
    elif q.total_amount != expected_amount:
        reason = f"amount={q.total_amount} expected={expected_amount}"
    elif q.invoice_payload != expected_payload:
        reason = f"payload={q.invoice_payload!r} expected={expected_payload!r}"
    elif is_ref and not await _has_unclaimed_referral(q.from_user.id):
        # Юзер пытается заплатить со скидкой, но у него нет валидного
        # реферала или скидка уже использована.
        reason = "no_unclaimed_referral"

    if reason is not None:
        log.warning(
            "pre_checkout_rejected",
            user=q.from_user.id,
            currency=q.currency,
            amount=q.total_amount,
            payload=q.invoice_payload,
            reason=reason,
        )
        await q.answer(
            ok=False,
            error_message="Счёт устарел или изменён. Открой /upgrade ещё раз.",
        )
        return

    log.info(
        "pre_checkout_ok",
        user=q.from_user.id,
        amount=q.total_amount,
        payload=q.invoice_payload,
    )
    await q.answer(ok=True)


@dp.message(F.successful_payment)
async def on_paid(message: Message) -> None:
    if message.from_user is None or message.successful_payment is None:
        return
    sp = message.successful_payment
    payload = sp.invoice_payload or ""
    settings = get_settings()
    try:
        days = int(payload.split(":")[1])
    except (ValueError, IndexError):
        days = settings.paid_tier_duration_days
    is_ref_payment = payload.endswith(":ref")

    now = datetime.now(UTC)
    reward_inviter: int | None = None  # tg_id если пора начислить бонус
    async with SessionLocal() as s:
        # Сначала фиксируем платёжку — без telegram_payment_charge_id
        # refund физически невозможен (Refund Policy §3 → refundStarsCharge).
        payment = Payment(
            charge_id=sp.telegram_payment_charge_id,
            user_tg_id=message.from_user.id,
            amount_stars=sp.total_amount,
            currency=sp.currency,
            payload=payload,
        )
        s.add(payment)
        try:
            await s.flush()
        except IntegrityError:
            # Дубликат успешного платежа (Telegram redelivery) — user уже
            # был апгрейжден прошлым проходом, не дописываем срок повторно.
            await s.rollback()
            log.warning(
                "payment_duplicate",
                user=message.from_user.id,
                charge=sp.telegram_payment_charge_id,
            )
            await message.answer("✅ Оплата уже учтена.")
            return

        user = await s.get(User, message.from_user.id)
        if user is None:
            user = User(
                tg_id=message.from_user.id,
                tier=Tier.PAID.value,
                paid_until=now + timedelta(days=days),
            )
            s.add(user)
        else:
            base = user.paid_until if user.paid_until and user.paid_until > now else now
            user.tier = Tier.PAID.value
            user.paid_until = base + timedelta(days=days)

        # Реферальный reward: только если payload ref, юзер действительно
        # инвайти и invitee_charge_id ещё пуст (защита от двойного бонуса).
        if is_ref_payment:
            ref = await s.get(Referral, message.from_user.id)
            if ref is not None and ref.invitee_charge_id is None:
                ref.invitee_charge_id = sp.telegram_payment_charge_id
                ref.rewarded_at = now
                inviter = await s.get(User, ref.inviter_tg_id)
                if inviter is not None:
                    base_i = (
                        inviter.paid_until
                        if inviter.paid_until and inviter.paid_until > now
                        else now
                    )
                    inviter.tier = Tier.PAID.value
                    inviter.paid_until = base_i + timedelta(
                        days=settings.referral_reward_days
                    )
                    reward_inviter = ref.inviter_tg_id
        await s.commit()

    await message.answer(
        "✅ Оплата получена. Лимит подписок увеличен, уведомления без задержки."
    )
    log.info(
        "payment_ok",
        user=message.from_user.id,
        days=days,
        charge=sp.telegram_payment_charge_id,
        amount=sp.total_amount,
        ref=is_ref_payment,
    )
    if reward_inviter is not None:
        log.info(
            "referral_rewarded",
            inviter=reward_inviter,
            invitee=message.from_user.id,
            days=settings.referral_reward_days,
        )
        try:
            await message.bot.send_message(
                reward_inviter,
                f"🎁 Друг по твоей ссылке оплатил paid! "
                f"+{settings.referral_reward_days} дней начислены.",
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "referral_notify_failed", inviter=reward_inviter, err=str(e)
            )


# ---------- reply-keyboard handlers ----------
# Регистрируются до fallback'а — иначе F.text-fallback проглотит текст кнопок.

@dp.message(F.text == BTN_FIND)
async def on_btn_find(message: Message) -> None:
    await message.answer(
        "Пришли адрес или название с командой /find перед ним.\n"
        "Пример: `/find Минск Пулихова`",
        parse_mode="Markdown",
    )


@dp.message(F.text == BTN_LIST)
async def on_btn_list(message: Message) -> None:
    if message.from_user is None:
        return
    await _send_list(message, message.from_user.id)


@dp.message(F.text == BTN_TIER)
async def on_btn_tier(message: Message) -> None:
    if message.from_user is None:
        return
    await _send_tier(message, message.from_user.id)


# ---------- settings (quiet hours) ----------

# Пресеты тихих часов: (label, from_hour, to_hour). Окно через полночь
# поддерживается — Notifier разруливает (см. quiet_hours.in_quiet_window).
_QH_PRESETS: list[tuple[str, int, int]] = [
    ("22:00 – 08:00", 22, 8),
    ("23:00 – 07:00", 23, 7),
    ("00:00 – 07:00", 0, 7),
]


def _fmt_qh(user: User) -> str:
    if user.quiet_from is None or user.quiet_to is None:
        return "выключены"
    return f"{user.quiet_from:%H:%M} – {user.quiet_to:%H:%M} ({user.tz})"


def _qh_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"qh:{a}:{b}")]
        for label, a, b in _QH_PRESETS
    ]
    rows.append([InlineKeyboardButton(text="🔕 Без тихих часов", callback_data="qh:off")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(F.text == BTN_SETTINGS)
async def on_btn_settings(message: Message) -> None:
    if message.from_user is None:
        return
    user = await ensure_user(message.from_user.id)
    text = (
        "<b>⚙️ Настройки</b>\n\n"
        f"🌙 Тихие часы: <b>{_fmt_qh(user)}</b>\n\n"
        "Выбери пресет — уведомления внутри окна откладываются до выхода:"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=_qh_kb())


# ---------- referrals ----------

@dp.message(F.text == BTN_REFERRAL)
async def on_btn_referral(message: Message) -> None:
    """Показ персональной реф-ссылки + статус (сколько пригласил, сколько
    наградилось).
    """
    if message.from_user is None or message.bot is None:
        return
    await ensure_user(message.from_user.id)
    me = await message.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{message.from_user.id}"
    s = get_settings()
    async with SessionLocal() as ses:
        total = (
            await ses.execute(
                select(func.count(Referral.invitee_tg_id)).where(
                    Referral.inviter_tg_id == message.from_user.id
                )
            )
        ).scalar_one()
        rewarded = (
            await ses.execute(
                select(func.count(Referral.invitee_tg_id)).where(
                    Referral.inviter_tg_id == message.from_user.id,
                    Referral.rewarded_at.is_not(None),
                )
            )
        ).scalar_one()
    text = (
        "🎁 <b>Приглашай друзей</b>\n\n"
        f"Друг получает первый paid со скидкой — <b>{s.referral_invitee_price_stars} ⭐</b> "
        f"вместо {s.paid_tier_price_stars} ⭐.\n"
        f"Ты получаешь <b>+{s.referral_reward_days} дней paid</b> "
        "за каждого, кто оплатил по твоей ссылке.\n\n"
        "Твоя ссылка (нажми, чтобы скопировать):\n"
        f"<code>{html.escape(link)}</code>\n\n"
        f"Приглашено: <b>{total}</b> · оплатили: <b>{rewarded}</b>"
    )
    await message.answer(text, parse_mode="HTML")


@dp.callback_query(F.data.startswith("qh:"))
async def on_qh_callback(cb: CallbackQuery) -> None:
    """Применить пресет тихих часов или выключить.

    callback_data: `qh:<from>:<to>` или `qh:off`.
    """
    if cb.from_user is None or cb.data is None or cb.message is None:
        return
    parts = cb.data.split(":")
    new_from: time | None = None
    new_to: time | None = None
    if parts == ["qh", "off"]:
        pass  # both None
    elif len(parts) == 3:
        try:
            a, b = int(parts[1]), int(parts[2])
        except ValueError:
            await cb.answer("Плохой пресет")
            return
        if not (0 <= a <= 23 and 0 <= b <= 23):
            await cb.answer("Плохой пресет")
            return
        new_from, new_to = time(a, 0), time(b, 0)
    else:
        await cb.answer("Плохой формат")
        return

    async with SessionLocal() as s:
        user = await s.get(User, cb.from_user.id)
        if user is None:
            await cb.answer("Юзер не найден — /start")
            return
        user.quiet_from = new_from
        user.quiet_to = new_to
        await s.commit()
        # Re-read для красивого фоллбэк-текста.
        await s.refresh(user)

    await cb.answer("Сохранено")
    await cb.message.edit_text(
        "<b>⚙️ Настройки</b>\n\n"
        f"🌙 Тихие часы: <b>{_fmt_qh(user)}</b>\n\n"
        "Можешь сменить пресет:",
        parse_mode="HTML",
        reply_markup=_qh_kb(),
    )


# ---------- admin ----------

@dp.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Сводка для админа: пользователи, подписки, платежи, доставки.

    Не-админы получают тот же путь, что и любой неизвестный текст — fallback.
    Молчим о существовании команды, чтобы не палить админ-инвентарь.
    """
    if message.from_user is None:
        return
    settings = get_settings()
    if message.from_user.id not in settings.admin_ids_set():
        return
    now = datetime.now(UTC)
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)
    d1 = now - timedelta(days=1)
    async with SessionLocal() as s:
        users_total = (await s.execute(select(func.count(User.tg_id)))).scalar_one()
        users_paid = (
            await s.execute(
                select(func.count(User.tg_id)).where(
                    User.tier == Tier.PAID.value, User.paid_until > now
                )
            )
        ).scalar_one()
        new_7d = (
            await s.execute(
                select(func.count(User.tg_id)).where(User.created_at > d7)
            )
        ).scalar_one()
        new_30d = (
            await s.execute(
                select(func.count(User.tg_id)).where(User.created_at > d30)
            )
        ).scalar_one()
        subs_total = (
            await s.execute(select(func.count(Subscription.id)))
        ).scalar_one()
        pay_7d = (
            await s.execute(
                select(func.count(Payment.charge_id)).where(Payment.paid_at > d7)
            )
        ).scalar_one()
        pay_30d = (
            await s.execute(
                select(func.count(Payment.charge_id)).where(Payment.paid_at > d30)
            )
        ).scalar_one()
        notif_24h = (
            await s.execute(
                select(func.count(NotificationLog.id)).where(
                    NotificationLog.delivered_at > d1
                )
            )
        ).scalar_one()
    users_free = users_total - users_paid
    text = (
        "<b>Stats</b>\n\n"
        f"<b>Users</b>: {users_total} (paid {users_paid} · free {users_free})\n"
        f"  new 7d: {new_7d} · 30d: {new_30d}\n\n"
        f"<b>Subs active</b>: {subs_total}\n\n"
        f"<b>Payments</b>: 7d {pay_7d} · 30d {pay_30d}\n\n"
        f"<b>Notifs delivered (24h)</b>: {notif_24h}"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("refund"))
async def cmd_refund(message: Message, command: CommandObject) -> None:
    """Refund последнего платежа юзера, downgrade до free, чистка подписок.

    Использование: /refund <tg_id>. Доступно только админам.

    Шаги:
      1. Найти последний `Payment` юзера с `refunded_at IS NULL`.
      2. Дёрнуть `bot.refund_star_payment` — если упадёт, БД не трогаем.
      3. Поставить `refunded_at`, `tier=free`, `paid_until=NULL`.
      4. Удалить все подписки кроме самой старой (free-лимит = 1).
         Cascade FK снесёт связанный notification_log сам.
      5. Уведомить юзера. Подтвердить админу.

    21-дневное окно refund'а — на стороне Telegram; если просрочено,
    `refund_star_payment` бросит исключение и мы откатимся.
    """
    if message.from_user is None:
        return
    settings = get_settings()
    if message.from_user.id not in settings.admin_ids_set():
        return

    raw = (command.args or "").strip()
    if not raw.isdigit():
        await message.answer("Использование: <code>/refund &lt;tg_id&gt;</code>", parse_mode="HTML")
        return
    target_tg = int(raw)

    async with SessionLocal() as s:
        user = await s.get(User, target_tg)
        if user is None:
            await message.answer(f"Юзер <code>{target_tg}</code> не найден.", parse_mode="HTML")
            return
        payment = (
            await s.execute(
                select(Payment)
                .where(
                    Payment.user_tg_id == target_tg,
                    Payment.refunded_at.is_(None),
                )
                .order_by(Payment.paid_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if payment is None:
            await message.answer(
                f"У юзера <code>{target_tg}</code> нет неотрефанженных платежей.",
                parse_mode="HTML",
            )
            return

        # Telegram refund API — сначала зовём её, только потом меняем БД.
        # Если упадёт (просрочено окно, сеть) — БД не тронем, юзер останется paid.
        try:
            await message.bot.refund_star_payment(
                user_id=target_tg,
                telegram_payment_charge_id=payment.charge_id,
            )
        except Exception as e:
            log.warning(
                "refund_api_failed",
                admin=message.from_user.id,
                target=target_tg,
                charge=payment.charge_id,
                err=str(e),
            )
            await message.answer(f"Refund API упал: <code>{html.escape(str(e))}</code>", parse_mode="HTML")
            return

        payment.refunded_at = datetime.now(UTC)
        user.tier = Tier.FREE.value
        user.paid_until = None

        # Реферальный reverse: если этот charge запустил начисление инвайтеру,
        # отнимаем `referral_reward_days` обратно. Если в результате
        # `paid_until <= now`, инвайтер сваливается в free (tier_reaper
        # подхватит, но мы и сами поставим — UX чище).
        ref = (
            await s.execute(
                select(Referral).where(Referral.invitee_charge_id == payment.charge_id)
            )
        ).scalar_one_or_none()
        reward_reversed_for: int | None = None
        if ref is not None:
            inviter = await s.get(User, ref.inviter_tg_id)
            if inviter is not None and inviter.paid_until is not None:
                inviter.paid_until -= timedelta(days=settings.referral_reward_days)
                now_ref = datetime.now(UTC)
                if inviter.paid_until <= now_ref:
                    inviter.tier = Tier.FREE.value
                    inviter.paid_until = None
                reward_reversed_for = ref.inviter_tg_id
            # Очищаем связку — повторно «не получится» дать бонус по тому же
            # charge'у (даже если он каким-то образом всплывёт).
            ref.invitee_charge_id = None
            ref.rewarded_at = None

        # Сохраняем самую старую подписку (created_at ASC, первый id),
        # удаляем остальные. NotificationLog → Subscription cascade FK
        # снесёт связанные строки.
        sub_ids = (
            await s.execute(
                select(Subscription.id)
                .where(Subscription.user_tg_id == target_tg)
                .order_by(Subscription.created_at.asc())
            )
        ).scalars().all()
        to_delete = list(sub_ids[1:])  # all except first (oldest)
        if to_delete:
            await s.execute(
                delete(Subscription).where(Subscription.id.in_(to_delete))
            )
        await s.commit()

    log.info(
        "refund_ok",
        admin=message.from_user.id,
        target=target_tg,
        charge=payment.charge_id,
        amount=payment.amount_stars,
        subs_deleted=len(to_delete),
        ref_reversed=reward_reversed_for,
    )
    if reward_reversed_for is not None:
        try:
            await message.bot.send_message(
                reward_reversed_for,
                f"⚠️ Платёж по твоей реферальной ссылке отменён. "
                f"−{settings.referral_reward_days} дней paid списаны.",
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "ref_reverse_notify_failed", inviter=reward_reversed_for, err=str(e)
            )

    # Уведомляем юзера. Если он удалил бота — Telegram бросит, не падаем.
    try:
        user_text = (
            "💸 <b>Возврат оформлен</b>\n\n"
            f"{payment.amount_stars} ⭐ вернулись на твой Stars-баланс.\n"
            "Тариф снят до free."
        )
        if to_delete:
            user_text += (
                f"\nПодписок удалено: {len(to_delete)}. "
                "Оставлена самая старая (free-лимит = 1)."
            )
        await message.bot.send_message(target_tg, user_text, parse_mode="HTML")
    except Exception as e:
        log.warning("refund_notify_failed", target=target_tg, err=str(e))

    await message.answer(
        f"✔ Refund <b>{payment.amount_stars} ⭐</b> → <code>{target_tg}</code>\n"
        f"Subs удалено: {len(to_delete)}",
        parse_mode="HTML",
    )


# ---------- fallback ----------

@dp.message(F.text)
async def fallback(message: Message) -> None:
    await message.answer("Не понял. /start — открыть меню.")


# ---------- runner ----------

async def _runner() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    if not settings.tg_bot_token:
        raise RuntimeError("TG_BOT_TOKEN is not set")
    bot = Bot(settings.tg_bot_token)
    # Slash-меню — только два пункта-якоря. Остальное теперь живёт в
    # persistent reply-клавиатуре, см. onboarding.main_reply_kb().
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать работу"),
        BotCommand(command="about", description="О боте и контакты"),
    ])
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    global _redis_instance
    _redis_instance = redis

    stop = asyncio.Event()

    def _sig(*_: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _sig)

    notifier = Notifier(bot, redis)
    bg_tasks = [
        asyncio.create_task(notifier.consume_events(stop), name="notifier-consume"),
        asyncio.create_task(notifier.delayed_worker(stop), name="notifier-delayed"),
        asyncio.create_task(tier_reaper(stop), name="tier-reaper"),
        asyncio.create_task(stale_claim_reaper(stop), name="stale-claim-reaper"),
    ]
    log.info("bot_start")
    try:
        await dp.start_polling(bot, handle_signals=False)
    finally:
        stop.set()
        for t in bg_tasks:
            t.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.gather(*bg_tasks, return_exceptions=True)
        await redis.aclose()
        await bot.session.close()
        log.info("bot_stopped")


def run() -> None:
    asyncio.run(_runner())


if __name__ == "__main__":
    run()
