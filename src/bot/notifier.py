"""Event consumer and notification dispatcher.

Pipeline:
    Redis Stream "charger:events"   (from poller)
        │
        ▼
    consume_events: XREADGROUP (consumer group "notifier-group")
        │
        ▼
    dispatch_event(event)
        - paid subscribers → Telegram immediately (rate-limited)
        - free subscribers → Redis ZSET "charger:notify:delayed"
                             with score = now + FREE_TIER_NOTIFY_DELAY_SEC
        │
        ▼
    delayed_worker: every 5s ZRANGEBYSCORE<=now → send and ZREM
        - before sending, also ZREM if the location became non-AVAILABLE
          meanwhile (canceling stale notifications)

Reliability invariants
----------------------

1. Cooldown is enforced via `notification_log.delivered_at IS NOT NULL`. We
   *claim* a row before sending (insert with delivered_at = NULL) and
   *commit* it (UPDATE delivered_at = now()) only on send success. On send
   failure we DELETE the claim — the cooldown is NOT consumed by failed
   deliveries. This prevents the "silent message loss" class of bug where a
   transient Telegram 5xx / VPN flake would lock the user out for the full
   cooldown window.

   The unique constraint (subscription_id, location_id, event_epoch) still
   protects against two concurrent dispatches racing for the same event —
   only one will succeed in inserting and the other will see the conflict
   and skip.

2. Stream consumption uses a Redis consumer group with explicit XACK so
   that messages enqueued while the bot was down are still delivered when
   it comes back up. The previous "$" cursor dropped every in-flight event
   between poller PUBLISH and bot restart.
"""
from __future__ import annotations

import asyncio
import contextlib
import html
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import orjson
import redis.asyncio as aioredis
import structlog
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiolimiter import AsyncLimiter
from sqlalchemy import and_, delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from bot.onboarding import operator_label
from config import get_settings
from db.models import (
    NotificationLog,
    Subscription,
    Tier,
    User,
)
from db.session import SessionLocal

if TYPE_CHECKING:
    from aiogram import Bot

log = structlog.get_logger(__name__)

EVENTS_STREAM = "charger:events"
DELAYED_ZSET = "charger:notify:delayed"
# Marker of the *current* status of a location — updated on every event.
# Used by the delayed-worker to cancel free notifications that became stale.
STATUS_HASH = "charger:location_status"

# Redis consumer group / consumer for restart-safe stream consumption.
NOTIFIER_GROUP = "notifier-group"
NOTIFIER_CONSUMER = "notifier-1"


def _status_icon(status: str | None) -> str:
    return {"AVAILABLE": "🟢", "FULLY_USED": "🔴", "UNAVAILABLE": "⚪"}.get(status or "", "❔")


def _quiet_until_utc(user: User, now_utc: datetime) -> datetime | None:
    """Если сейчас в окне тихих часов — момент выхода в UTC. Иначе None.

    Поддерживается окно через полночь (`quiet_from > quiet_to`). Если оба
    поля NULL или равны — тихих часов нет.
    """
    if user.quiet_from is None or user.quiet_to is None:
        return None
    qf, qt = user.quiet_from, user.quiet_to
    if qf == qt:
        return None  # пустое окно
    try:
        tz = ZoneInfo(user.tz or "Europe/Minsk")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("Europe/Minsk")
    now_local = now_utc.astimezone(tz)
    cur_t = now_local.time()
    wraps = qf > qt
    in_window = (cur_t >= qf or cur_t < qt) if wraps else (qf <= cur_t < qt)
    if not in_window:
        return None
    candidate = now_local.replace(
        hour=qt.hour, minute=qt.minute, second=0, microsecond=0
    )
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def _format_alert(event: dict) -> str:
    """HTML-форматирование с эскейпом name/address.

    Markdown ломается на любой `_`/`*`/`[` в адресе, и тогда send падает,
    cooldown release-ится, на следующем переходе всё повторяется — silent
    loss для конкретной локации навсегда. HTML escape отрезает этот класс.

    `operator_label` маппит internal-id (`central`/`evika`/`battery-fly`)
    в брендовое имя (`Маланка`/`Evika`/`Battery-fly`).

    Какие коннекторы показываем в строке «🔌»:
      - предпочитаем `transitioned_connector_types` — что ИМЕННО только что
        освободилось (Chademo, не «Type2 который уже час свободен»);
      - fallback на `free_connector_types` — для legacy событий до того как
        poller начал отдавать `transitioned_connector_types`;
      - пустой список = poller не смог получить detail, типы неизвестны —
        строку «🔌» пропускаем.
    """
    name = html.escape(str(event.get("name", "")))
    address = html.escape(str(event.get("address", "")))
    operator = html.escape(operator_label(event.get("operator")))
    raw_transitioned = event.get("transitioned_connector_types") or []
    raw_free = event.get("free_connector_types") or []
    shown = raw_transitioned if isinstance(raw_transitioned, list) and raw_transitioned else raw_free
    connector_line = ""
    if isinstance(shown, list) and shown:
        types = ", ".join(html.escape(str(t)) for t in shown if t)
        if types:
            connector_line = f"🔌 {types}\n"
    return (
        f"🟢 Освободилась: <b>{name}</b>\n"
        f"{address}\n"
        f"{connector_line}"
        f"Сеть: {operator} · 📍 {event['lat']:.5f}, {event['lon']:.5f}"
    )


def _unsub_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔕 Отписаться от этой локации",
                    callback_data=f"unsub:{subscription_id}",
                )
            ]
        ]
    )


class Notifier:
    def __init__(self, bot: Bot, redis: aioredis.Redis) -> None:
        self.bot = bot
        self.redis = redis
        self.settings = get_settings()
        # Telegram global limit ~30 msg/sec. Keep headroom, per user 1 msg/sec max.
        self._limiter = AsyncLimiter(self.settings.tg_send_rate_per_sec, 1)

    # -------- sending --------

    async def _send(
        self,
        tg_id: int,
        text: str,
        keyboard: InlineKeyboardMarkup | None,
        parse_mode: str | None = "HTML",
        silent: bool = False,
    ) -> bool:
        """Send one message. Returns True iff Telegram accepted it.

        Контракт ошибок (важно для классификации silent loss):

        - ``TelegramForbiddenError`` — юзер заблокировал бота / удалил чат.
          Дальше слать ему бесполезно; возвращаем False (caller освободит
          claim). Подписки не удаляем — пусть отвалятся естественно по
          quota или по /delete_me.
        - ``TelegramBadRequest`` — мы сами зафакапили payload (битый HTML
          в имени локации, длиннющий keyboard, etc). Логируем громко с
          превью текста — без этого silent loss трудно диагностировать.
        - ``TelegramRetryAfter`` — flood-wait. Спим ВНЕ self._limiter,
          иначе один тяжёлый юзер тормозит всю очередь на retry_after сек.
          После сна — ровно одна повторная попытка.
        - ``TelegramNetworkError`` — VPN/gluetun моргнул. Одна повторка
          через короткий sleep — обычно проходит на втором ударе.
        """
        try:
            async with self._limiter:
                await self.bot.send_message(
                    tg_id,
                    text,
                    parse_mode=parse_mode,
                    reply_markup=keyboard,
                    disable_notification=silent,
                )
            return True
        except TelegramForbiddenError as e:
            log.info("tg_user_blocked", user=tg_id, err=str(e))
            return False
        except TelegramBadRequest as e:
            log.warning(
                "tg_bad_request",
                user=tg_id,
                err=str(e),
                parse_mode=parse_mode,
                preview=text[:200],
            )
            return False
        except TelegramRetryAfter as e:
            log.warning("tg_flood_wait", user=tg_id, retry_after=e.retry_after)
            # КРИТИЧЕСКИ важно — sleep СНАРУЖИ limiter'а, иначе один
            # flood-wait (30-300 сек) застопорит весь pipeline отправки.
            await asyncio.sleep(e.retry_after + 1)
        except TelegramNetworkError as e:
            log.warning("tg_network_error", user=tg_id, err=str(e))
            await asyncio.sleep(1.0)
        except Exception as e:  # noqa: BLE001
            # Ловим всё прочее (баги aiogram, кодеки) — но громко, не warn.
            log.exception("tg_send_failed", user=tg_id, err=str(e))
            return False

        # Сюда попадаем только из RetryAfter / NetworkError — одна повторка.
        try:
            async with self._limiter:
                await self.bot.send_message(
                    tg_id,
                    text,
                    parse_mode=parse_mode,
                    reply_markup=keyboard,
                    disable_notification=silent,
                )
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("tg_send_failed_after_retry", user=tg_id, err=str(e))
            return False

    # -------- dedup/cooldown --------

    async def _claim_slot(
        self, sub_id: int, loc_id: int, event_epoch: int
    ) -> bool:
        """Reserve the (sub, loc, event) slot.

        Returns True iff the caller now owns the row and should attempt to
        send. False means either:
          - cooldown is currently active for a *delivered* row, OR
          - some concurrent dispatcher already claimed this exact event.

        The row is inserted with ``delivered_at = NULL``. Cooldown is only
        enforced against rows where ``delivered_at IS NOT NULL`` so that a
        failed send (followed by ``release_slot``) does not poison subsequent
        legitimate retries.
        """
        cutoff = datetime.now(UTC) - timedelta(
            seconds=self.settings.notify_cooldown_sec
        )
        async with SessionLocal() as s:
            # Cooldown applies only to *successful* prior deliveries.
            recent = (
                await s.execute(
                    select(NotificationLog.id)
                    .where(
                        and_(
                            NotificationLog.subscription_id == sub_id,
                            NotificationLog.location_id == loc_id,
                            NotificationLog.delivered_at.is_not(None),
                            NotificationLog.delivered_at >= cutoff,
                        )
                    )
                    .limit(1)
                )
            ).first()
            if recent is not None:
                return False  # cooldown still active

            ins = (
                pg_insert(NotificationLog)
                .values(
                    subscription_id=sub_id,
                    location_id=loc_id,
                    event_epoch=event_epoch,
                    delivered_at=None,
                )
                .on_conflict_do_nothing(
                    index_elements=["subscription_id", "location_id", "event_epoch"]
                )
            )
            result = await s.execute(ins)
            await s.commit()
            return result.rowcount > 0

    async def _commit_delivery(
        self, sub_id: int, loc_id: int, event_epoch: int
    ) -> None:
        """Mark a previously claimed slot as delivered (cooldown now active)."""
        async with SessionLocal() as s:
            await s.execute(
                update(NotificationLog)
                .where(
                    and_(
                        NotificationLog.subscription_id == sub_id,
                        NotificationLog.location_id == loc_id,
                        NotificationLog.event_epoch == event_epoch,
                    )
                )
                .values(delivered_at=datetime.now(UTC))
            )
            await s.commit()

    async def _release_slot(
        self, sub_id: int, loc_id: int, event_epoch: int
    ) -> None:
        """Release a claim because the send failed.

        We DELETE rather than NULL-out a flag, because keeping an
        undelivered row around would be visually indistinguishable from a
        live claim by another worker, and would block legitimate retries on
        the next event.
        """
        async with SessionLocal() as s:
            await s.execute(
                delete(NotificationLog).where(
                    and_(
                        NotificationLog.subscription_id == sub_id,
                        NotificationLog.location_id == loc_id,
                        NotificationLog.event_epoch == event_epoch,
                        NotificationLog.delivered_at.is_(None),
                    )
                )
            )
            await s.commit()

    async def _send_with_slot(
        self,
        tg_id: int,
        sub_id: int,
        loc_id: int,
        event_epoch: int,
        text: str,
        location_name: str,
        silent: bool = False,
    ) -> bool:
        """Send + book-keep the cooldown slot atomically wrt failure.

        On success: UPDATE delivered_at -> now() (cooldown begins),
        инкремент notify_count и проверка квоты (если исчерпана — sub
        удаляется и шлётся пуш «подпишись заново»).
        On failure: DELETE the claim row so the next event isn't blocked.

        ``silent`` пробрасывается в Telegram `disable_notification` — нужно
        для тихих часов юзера: сообщение всё равно приходит в реальном
        времени, но без звука/вибрации.
        """
        ok = await self._send(tg_id, text, _unsub_keyboard(sub_id), silent=silent)
        if not ok:
            await self._release_slot(sub_id, loc_id, event_epoch)
            return False
        await self._commit_delivery(sub_id, loc_id, event_epoch)
        if await self._increment_quota_and_check(sub_id):
            await self._handle_quota_exhausted(tg_id, sub_id, loc_id, location_name)
        return True

    async def _increment_quota_and_check(self, sub_id: int) -> bool:
        """+1 к notify_count подписки. True если квота исчерпана."""
        async with SessionLocal() as s:
            result = await s.execute(
                update(Subscription)
                .where(Subscription.id == sub_id)
                .values(notify_count=Subscription.notify_count + 1)
                .returning(Subscription.notify_count, Subscription.notify_limit)
            )
            row = result.first()
            await s.commit()
        if row is None:
            return False
        # row — Row[(notify_count, notify_limit)]
        count, limit = row[0], row[1]
        return limit is not None and count >= limit

    async def _handle_quota_exhausted(
        self, tg_id: int, sub_id: int, loc_id: int, location_name: str
    ) -> None:
        """Удаляем подписку и шлём пуш «лимит исчерпан, [Подписаться снова]»."""
        async with SessionLocal() as s:
            await s.execute(delete(Subscription).where(Subscription.id == sub_id))
            await s.commit()
        text = (
            f"📭 Лимит уведомлений по «{location_name}» исчерпан — "
            f"подписка снята.\nХочешь продолжить — оформи заново."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔄 Подписаться снова",
                        callback_data=f"sub:{loc_id}",
                    )
                ]
            ]
        )
        # parse_mode=None — в имени локации могут быть символы, ломающие
        # Markdown (звёздочки, подчёркивания), а exhausted-пуш категорически
        # не должен теряться: это финальный аккорд подписки.
        await self._send(tg_id, text, kb, parse_mode=None)
        log.info("quota_exhausted_notified", user=tg_id, sub=sub_id, loc=loc_id)

    # -------- event dispatch --------

    async def dispatch_event(self, event: dict) -> None:
        loc_id = int(event["location_id"])
        to_status = event.get("to_status")

        # Maintain a "current status" hash so the delayed-worker can cancel
        # free notifications whose locations re-filled in the delay window.
        if isinstance(to_status, str):
            await self.redis.hset(STATUS_HASH, str(loc_id), to_status)

        if not event.get("became_available"):
            return

        async with SessionLocal() as s:
            subs = (
                await s.execute(
                    select(Subscription, User)
                    .join(User, User.tg_id == Subscription.user_tg_id)
                    .where(Subscription.location_id == loc_id)
                )
            ).all()

        if not subs:
            return

        now = datetime.now(UTC)
        event_epoch = int(event["ts"])
        text = _format_alert(event)
        location_name = str(event.get("name", "локация"))

        # Какие именно типы коннекторов ОСВОБОДИЛИСЬ в этом событии.
        # Poller отдаёт `transitioned_connector_types` — diff между двумя
        # кадрами SSE (или агрегатный flip для REST). Это то, что реально
        # переключилось в Available, а не «сейчас свободно вообще» — иначе
        # подписчик на Chademo получал бы пуш, когда освободился Type2.
        # Fallback на `free_connector_types` — для событий, опубликованных
        # старым poller'ом до этого фикса (rolling deploy).
        raw_transitioned = event.get("transitioned_connector_types")
        if not isinstance(raw_transitioned, list) or not raw_transitioned:
            raw_transitioned = event.get("free_connector_types") or []
        transitioned_set: set[str] = (
            set(raw_transitioned) if isinstance(raw_transitioned, list) else set()
        )

        # (tg_id, sub_id, silent) — silent=True значит «сейчас тихие часы»,
        # шлём с disable_notification (без звука/вибрации). НЕ откладываем
        # доставку — push приходит сразу, просто беззвучно.
        paid_now: list[tuple[int, int, bool]] = []
        # Free идут в delayed-queue с обычной задержкой; silent определяется
        # уже при доставке (на тот момент юзер мог войти/выйти из окна).
        free_queue: list[tuple[int, int]] = []
        for sub, user in subs:
            # Фильтр по типу коннектора — только если sub его задал И poller
            # знает, какие коннекторы реально только что освободились. Если
            # transitioned_set пуст (legacy event без поля и без free types)
            # — фильтр не применяем (лучше false-positive чем silent loss).
            if (
                sub.connector_type is not None
                and transitioned_set
                and sub.connector_type not in transitioned_set
            ):
                continue
            # auto-downgrade expired paid tier
            tier = user.tier
            if tier == Tier.PAID.value and user.paid_until and user.paid_until < now:
                tier = Tier.FREE.value
            if not await self._claim_slot(sub.id, loc_id, event_epoch):
                continue
            if tier == Tier.PAID.value:
                silent = _quiet_until_utc(user, now) is not None
                paid_now.append((user.tg_id, sub.id, silent))
            else:
                free_queue.append((user.tg_id, sub.id))

        # paid → send now (silent если сейчас тихие часы у юзера).
        for tg_id, sub_id, silent in paid_now:
            await self._send_with_slot(
                tg_id, sub_id, loc_id, event_epoch, text, location_name, silent=silent
            )

        # free → schedule via Redis ZSET; the slot stays "claimed" until the
        # delayed_worker either delivers (commit) or cancels/fails (release).
        # Silent-флаг считаем в момент доставки — на тот момент окно тихих
        # часов могло закрыться (например, событие случилось в 06:58, окно
        # 23-07, free_delay=2мин → доставка в 07:00 уже громкая).
        if free_queue:
            delay_ts = int(now.timestamp()) + self.settings.free_tier_notify_delay_sec
            payload = {
                "tg_ids_subs": free_queue,
                "text": text,
                "location_id": loc_id,
                "location_name": location_name,
                "event_epoch": event_epoch,
            }
            await self.redis.zadd(
                DELAYED_ZSET, {orjson.dumps(payload).decode(): delay_ts}
            )

    # -------- delayed worker --------

    async def delayed_worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            now = int(datetime.now(UTC).timestamp())
            due = await self.redis.zrangebyscore(DELAYED_ZSET, 0, now, start=0, num=100)
            for raw in due:
                # atomic "take": remove then process. If already taken by
                # another worker, zrem returns 0 and we skip.
                removed = await self.redis.zrem(DELAYED_ZSET, raw)
                if removed == 0:
                    continue
                try:
                    payload = orjson.loads(raw)
                except Exception as e:  # noqa: BLE001
                    # ZREM уже снял запись — payload потерян. Без warning'а
                    # это была бы немая потеря целой пачки free-уведомлений.
                    log.warning(
                        "delayed_unparseable",
                        err=str(e),
                        preview=str(raw)[:200],
                    )
                    continue

                loc_id = int(payload["location_id"])
                event_epoch = int(payload["event_epoch"])

                # cancel if meanwhile the location became non-AVAILABLE.
                # IMPORTANT: when cancelling we must RELEASE the cooldown
                # claims for every subscription, otherwise the next
                # legitimate AVAILABLE within the cooldown window would be
                # silently swallowed.
                cur_status = await self.redis.hget(STATUS_HASH, str(loc_id))
                if cur_status and cur_status != "AVAILABLE":
                    log.info(
                        "free_notify_cancelled",
                        location=loc_id,
                        current_status=cur_status,
                    )
                    for _tg, sub_id in payload["tg_ids_subs"]:
                        await self._release_slot(sub_id, loc_id, event_epoch)
                    continue

                text = payload["text"]
                location_name = payload.get("location_name") or "локация"
                cutoff = datetime.now(UTC) - timedelta(
                    seconds=self.settings.notify_cooldown_sec
                )
                for tg_id, sub_id in payload["tg_ids_subs"]:
                    # Перепроверяем cooldown в момент доставки: пока запись
                    # лежала в delayed-queue, могло прилететь и доставиться
                    # другое событие по той же подписке — тогда не дублируем.
                    async with SessionLocal() as s:
                        sub = await s.get(Subscription, sub_id)
                        if sub is None:
                            # Подписка снята (квота исчерпана / unsubscribe /
                            # delete_me); cascade FK уже снёс наш claim.
                            log.info(
                                "delayed_skip_sub_gone", sub=sub_id, loc=loc_id
                            )
                            continue
                        recent = (
                            await s.execute(
                                select(NotificationLog.id)
                                .where(
                                    and_(
                                        NotificationLog.subscription_id == sub_id,
                                        NotificationLog.location_id == loc_id,
                                        NotificationLog.delivered_at.is_not(None),
                                        NotificationLog.delivered_at >= cutoff,
                                    )
                                )
                                .limit(1)
                            )
                        ).first()
                        user = await s.get(User, tg_id)
                    if recent is not None:
                        log.info(
                            "delayed_skip_cooldown", sub=sub_id, loc=loc_id
                        )
                        await self._release_slot(sub_id, loc_id, event_epoch)
                        continue
                    silent = (
                        user is not None
                        and _quiet_until_utc(user, datetime.now(UTC)) is not None
                    )
                    await self._send_with_slot(
                        tg_id, sub_id, loc_id, event_epoch, text, location_name,
                        silent=silent,
                    )

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=5.0)

    # -------- main loop --------

    async def _ensure_consumer_group(self) -> None:
        """Create the consumer group if it doesn't exist.

        ``mkstream=True`` means the stream is created on demand if the
        notifier boots before the poller has produced its first event.
        Subsequent calls return BUSYGROUP — we swallow that.
        """
        try:
            await self.redis.xgroup_create(
                EVENTS_STREAM, NOTIFIER_GROUP, id="0", mkstream=True
            )
            log.info("xgroup_created", stream=EVENTS_STREAM, group=NOTIFIER_GROUP)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def consume_events(self, stop: asyncio.Event) -> None:
        """XREADGROUP loop — consume events into dispatch_event.

        Restart-safe: any messages produced while the consumer was down (or
        produced & not yet acked on a previous crash) are replayed via the
        ">"" cursor / pending-list semantics of consumer groups.

        We first drain anything that was claimed by this consumer name on a
        previous run but never acked (id "0"), then switch to fresh-only
        (">"") for normal operation.
        """
        await self._ensure_consumer_group()

        # On startup, drain pending entries for this consumer (if any) so we
        # don't miss anything that was delivered to us but not acked before
        # the previous crash.
        cursor = "0"
        while not stop.is_set():
            try:
                resp = await self.redis.xreadgroup(
                    NOTIFIER_GROUP,
                    NOTIFIER_CONSUMER,
                    {EVENTS_STREAM: cursor},
                    block=5_000,
                    count=50,
                )
            except aioredis.ResponseError as e:
                # Group was deleted out from under us; recreate.
                if "NOGROUP" in str(e):
                    log.warning("xreadgroup_nogroup", err=str(e))
                    await self._ensure_consumer_group()
                    continue
                log.warning("xreadgroup_failed", err=str(e))
                await asyncio.sleep(1)
                continue
            except Exception as e:  # noqa: BLE001
                log.warning("xreadgroup_failed", err=str(e))
                await asyncio.sleep(1)
                continue

            if not resp:
                # Pending list drained (or no live entries within block).
                # Either way we want the live tail next.
                cursor = ">"
                continue

            total_entries = 0
            for _, entries in resp:
                total_entries += len(entries)
                for msg_id, fields in entries:
                    try:
                        event = orjson.loads(fields["data"])
                    except Exception as e:  # noqa: BLE001
                        # Unparseable — ack to drop, otherwise we'd retry forever.
                        # Громко логируем: silent ack без warning'а скрывал бы
                        # факт потери целого события — а это ровно тот класс
                        # багов, против которого мы строим всю reliability-обвязку.
                        log.warning(
                            "stream_unparseable",
                            msg_id=msg_id,
                            err=str(e),
                            preview=str(fields.get("data", ""))[:200],
                        )
                        await self.redis.xack(EVENTS_STREAM, NOTIFIER_GROUP, msg_id)
                        continue
                    try:
                        await self.dispatch_event(event)
                    except Exception as e:  # noqa: BLE001
                        log.exception("dispatch_failed", err=str(e))
                        # Do NOT ack on dispatch failure — the entry stays
                        # in the pending list and will be retried on the
                        # next loop iteration / restart.
                        continue
                    await self.redis.xack(EVENTS_STREAM, NOTIFIER_GROUP, msg_id)

            # Once the pending list yields nothing new, move to live tail.
            if cursor == "0" and total_entries == 0:
                cursor = ">"


# Окно, в течение которого «висячий» claim считается живым in-flight'ом,
# а не последствием краша. Нормальная отправка занимает <1 сек, 5 минут —
# с большим запасом даже под TG flood-wait (60-300 сек). Тика reaper'а
# тоже 5 минут — частить нет смысла, claims редкие.
_STALE_CLAIM_CUTOFF_MIN = 5
_STALE_CLAIM_REAPER_INTERVAL_SEC = 300


async def stale_claim_reaper(stop: asyncio.Event) -> None:
    """Удаляет «висячие» notification_log claims, оставшиеся после крашей.

    Claim — строка `(sub, loc, epoch, delivered_at=NULL)`, вставленная до
    отправки и которую должен был commit'нуть (`UPDATE delivered_at`) или
    удалить (`DELETE`) тот же воркер. Если процесс упал между INSERT и
    одним из них — строка остаётся вечно, и следующий INSERT по тому же
    `(sub, loc, epoch)` ловит unique-конфликт, dispatch_event тихо
    пропускает уведомление.

    Сценарий редкий (нужен crash между двумя async DB-вызовами), но
    «тихая потеря» — топ-1 класс багов для этого бота. Этот reaper
    закрывает класс целиком: через ≤cutoff минут висячая строка
    удаляется, следующий INSERT проходит, доставка наверстаёт.
    """
    while not stop.is_set():
        try:
            async with SessionLocal() as s:
                cutoff = datetime.now(UTC) - timedelta(
                    minutes=_STALE_CLAIM_CUTOFF_MIN
                )
                result = await s.execute(
                    delete(NotificationLog).where(
                        and_(
                            NotificationLog.delivered_at.is_(None),
                            NotificationLog.sent_at < cutoff,
                        )
                    )
                )
                await s.commit()
                if result.rowcount:
                    log.info("stale_claims_reaped", count=result.rowcount)
        except Exception as e:  # noqa: BLE001
            log.exception("stale_reaper_failed", err=str(e))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                stop.wait(), timeout=_STALE_CLAIM_REAPER_INTERVAL_SEC
            )


async def tier_reaper(stop: asyncio.Event) -> None:
    """Periodically demote expired paid users to free tier in the DB.

    The notifier path already handles on-the-fly demotion, so this is mostly
    bookkeeping for `/status` and subscription-limit checks.
    """
    settings = get_settings()
    while not stop.is_set():
        now = datetime.now(UTC)
        async with SessionLocal() as s:
            rows = (
                await s.execute(
                    select(User).where(
                        and_(
                            User.tier == Tier.PAID.value,
                            User.paid_until.is_not(None),
                            User.paid_until < now,
                        )
                    )
                )
            ).scalars().all()
            for u in rows:
                u.tier = Tier.FREE.value
            if rows:
                await s.commit()
                log.info("tier_reaper_downgraded", count=len(rows))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.tier_reaper_interval_sec)
