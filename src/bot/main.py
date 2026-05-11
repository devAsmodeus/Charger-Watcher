from __future__ import annotations

import asyncio
import contextlib
import html
import signal
from datetime import datetime, timedelta, timezone

import orjson
import redis.asyncio as aioredis
import structlog
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
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
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from bot.geo import find_nearby
from bot.notifier import Notifier, tier_reaper
from bot.onboarding import (
    GREETING_NEW,
    GREETING_RETURNING,
    about_text,
    onboarding_kb,
)
from config import get_settings
from db.models import (
    Location,
    NotificationLog,
    Payment,
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
        and user.paid_until < datetime.now(timezone.utc)
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

@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if message.from_user is None:
        return
    _, is_new = await ensure_user_with_flag(message.from_user.id)
    greeting = GREETING_NEW if is_new else GREETING_RETURNING
    await message.answer(greeting, reply_markup=onboarding_kb())


@dp.message(Command("privacy"))
async def cmd_privacy(message: Message) -> None:
    await message.answer(
        "*Политика конфиденциальности*\n\n"
        "Сервис хранит минимум данных:\n"
        "• Ваш Telegram ID (без имени/телефона)\n"
        "• Список ваших подписок (id локации)\n"
        "• Лог отправленных уведомлений (для дедупа)\n\n"
        "Данные не передаются третьим лицам и используются только для рассылки уведомлений. "
        "Удалить все свои данные можно командой /delete_me — действие необратимо.\n\n"
        "Сервис не является официальным от оператора зарядных станций. "
        "Вопросы и жалобы — через /start → контакты автора.",
        parse_mode="Markdown",
    )


@dp.message(Command("delete_me"))
async def cmd_delete_me(message: Message) -> None:
    if message.from_user is None:
        return
    tg_id = message.from_user.id
    async with SessionLocal() as s:
        # FK cascades will handle subs + notif log
        await s.execute(delete(NotificationLog).where(
            NotificationLog.subscription_id.in_(
                select(Subscription.id).where(Subscription.user_tg_id == tg_id)
            )
        ))
        await s.execute(delete(Subscription).where(Subscription.user_tg_id == tg_id))
        await s.execute(delete(User).where(User.tg_id == tg_id))
        await s.commit()
    await message.answer("Все данные удалены. /start — начать заново.")


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
    if cb.from_user is None or cb.data is None or cb.message is None:
        return
    action = cb.data.split(":", 1)[1]
    msg = cb.message  # the bot's greeting message; reply via .answer()

    if action == "nearby":
        await cb.answer()
        await _send_nearby_prompt(msg)
        return
    if action == "find":
        await cb.answer()
        await msg.answer(
            "Пришли адрес или название одним сообщением и команду /find перед ним.\n"
            "Пример: `/find Минск Пулихова`",
            parse_mode="Markdown",
        )
        return
    if action == "list":
        await cb.answer()
        await _send_list(msg, cb.from_user.id)
        return
    if action == "upgrade":
        await cb.answer()
        await _send_upgrade_invoice(msg)
        return
    if action == "about":
        await cb.answer()
        await msg.answer(about_text(), parse_mode="HTML")
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


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    if message.from_user is None:
        return
    user = await ensure_user(message.from_user.id)
    tier = _effective_tier(user)
    lines = [f"Тариф: *{tier}*"]
    if user.paid_until:
        lines.append(f"Действует до: {user.paid_until:%Y-%m-%d %H:%M UTC}")
    lines.append(f"Лимит подписок: {tier_limit(tier)}")
    if tier == Tier.FREE.value:
        s = get_settings()
        lines.append(
            f"\n/upgrade — {s.paid_tier_duration_days} дней за ⭐️{s.paid_tier_price_stars}"
        )
    await message.answer("\n".join(lines), parse_mode="Markdown")


async def _send_upgrade_invoice(message: Message) -> None:
    s = get_settings()
    await message.answer_invoice(
        title="Charger Watcher — Paid",
        description=(
            f"{s.paid_tier_duration_days} дней: до {s.paid_tier_max_subscriptions} подписок, "
            "мгновенные уведомления без задержки."
        ),
        prices=[LabeledPrice(label="Подписка", amount=s.paid_tier_price_stars)],
        currency="XTR",
        payload=f"paid:{s.paid_tier_duration_days}",
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
    """
    s = get_settings()
    expected_payload = f"paid:{s.paid_tier_duration_days}"
    reason: str | None = None
    if q.currency != "XTR":
        reason = f"currency={q.currency}"
    elif q.total_amount != s.paid_tier_price_stars:
        reason = f"amount={q.total_amount} expected={s.paid_tier_price_stars}"
    elif q.invoice_payload != expected_payload:
        reason = f"payload={q.invoice_payload!r} expected={expected_payload!r}"

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
    try:
        days = int(payload.split(":", 1)[1])
    except (ValueError, IndexError):
        days = get_settings().paid_tier_duration_days

    now = datetime.now(timezone.utc)
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
    )


@dp.message(F.text)
async def fallback(message: Message) -> None:
    await message.answer("Не понял. Набери /start для списка команд.")


# ---------- runner ----------

async def _runner() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    if not settings.tg_bot_token:
        raise RuntimeError("TG_BOT_TOKEN is not set")
    bot = Bot(settings.tg_bot_token)
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    global _redis_instance
    _redis_instance = redis

    stop = asyncio.Event()

    def _sig(*_: object) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _sig)
        except NotImplementedError:
            pass

    notifier = Notifier(bot, redis)
    bg_tasks = [
        asyncio.create_task(notifier.consume_events(stop), name="notifier-consume"),
        asyncio.create_task(notifier.delayed_worker(stop), name="notifier-delayed"),
        asyncio.create_task(tier_reaper(stop), name="tier-reaper"),
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
