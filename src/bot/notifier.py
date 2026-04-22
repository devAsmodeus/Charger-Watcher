"""Event consumer and notification dispatcher.

Pipeline:
    Redis Stream "charger:events"   (from poller)
        │
        ▼
    notifier_loop(xread, block=5s)
        │
        ▼
    _dispatch_event(event)
        - paid subscribers → Telegram immediately (rate-limited)
        - free subscribers → Redis ZSET "charger:notify:delayed"
                             with score = now + FREE_TIER_NOTIFY_DELAY_SEC
        │
        ▼
    delayed_worker: every 5s ZRANGEBYSCORE<=now → send and ZREM
        - before sending, also ZREM if the location became non-AVAILABLE
          meanwhile (canceling stale notifications)

Dedup via notification_log(subscription_id, location_id, event_epoch).
Cooldown via notification_log.sent_at > now() - NOTIFY_COOLDOWN_SEC.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import orjson
import redis.asyncio as aioredis
import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiolimiter import AsyncLimiter
from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import get_settings
from db.models import (
    Location,
    NotificationLog,
    Subscription,
    Tier,
    User,
)
from db.session import SessionLocal

log = structlog.get_logger(__name__)

EVENTS_STREAM = "charger:events"
DELAYED_ZSET = "charger:notify:delayed"
# Marker of the *current* status of a location — updated on every event.
# Used by the delayed-worker to cancel free notifications that became stale.
STATUS_HASH = "charger:location_status"


def _status_icon(status: str | None) -> str:
    return {"AVAILABLE": "🟢", "FULLY_USED": "🔴", "UNAVAILABLE": "⚪"}.get(status or "", "❔")


def _format_alert(event: dict) -> str:
    return (
        f"🟢 Освободилась: *{event['name']}*\n"
        f"{event['address']}\n"
        f"Сеть: {event['operator']} · 📍 {event['lat']:.5f}, {event['lon']:.5f}"
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
        self._last_stream_id = "$"

    # -------- sending --------

    async def _send(self, tg_id: int, text: str, keyboard: InlineKeyboardMarkup | None) -> bool:
        async with self._limiter:
            try:
                await self.bot.send_message(
                    tg_id, text, parse_mode="Markdown", reply_markup=keyboard
                )
                return True
            except TelegramRetryAfter as e:
                log.warning("tg_flood_wait", user=tg_id, retry_after=e.retry_after)
                await asyncio.sleep(e.retry_after + 1)
                try:
                    await self.bot.send_message(
                        tg_id, text, parse_mode="Markdown", reply_markup=keyboard
                    )
                    return True
                except Exception as e2:  # noqa: BLE001
                    log.warning("tg_send_failed_after_retry", user=tg_id, err=str(e2))
                    return False
            except Exception as e:  # noqa: BLE001
                log.warning("tg_send_failed", user=tg_id, err=str(e))
                return False

    # -------- dedup/cooldown --------

    async def _can_notify(
        self, sub_id: int, loc_id: int, event_epoch: int
    ) -> bool:
        """True iff there's no dedup/cooldown violation for (sub, loc).

        Atomic: inside one transaction we check cooldown and try to insert
        the log row. If either check fails, we don't emit a notification.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=self.settings.notify_cooldown_sec
        )
        async with SessionLocal() as s:
            recent = (
                await s.execute(
                    select(NotificationLog.id)
                    .where(
                        and_(
                            NotificationLog.subscription_id == sub_id,
                            NotificationLog.location_id == loc_id,
                            NotificationLog.sent_at >= cutoff,
                        )
                    )
                    .limit(1)
                )
            ).first()
            if recent is not None:
                return False  # cooldown

            ins = (
                pg_insert(NotificationLog)
                .values(
                    subscription_id=sub_id,
                    location_id=loc_id,
                    event_epoch=event_epoch,
                )
                .on_conflict_do_nothing(
                    index_elements=["subscription_id", "location_id", "event_epoch"]
                )
            )
            result = await s.execute(ins)
            await s.commit()
            return result.rowcount > 0

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

        now = datetime.now(timezone.utc)
        event_epoch = int(event["ts"])
        text = _format_alert(event)

        paid_ready: list[tuple[int, int]] = []  # (tg_id, sub_id)
        free_ready: list[tuple[int, int]] = []
        for sub, user in subs:
            # auto-downgrade expired paid tier
            tier = user.tier
            if tier == Tier.PAID.value and user.paid_until and user.paid_until < now:
                tier = Tier.FREE.value
            if not await self._can_notify(sub.id, loc_id, event_epoch):
                continue
            if tier == Tier.PAID.value:
                paid_ready.append((user.tg_id, sub.id))
            else:
                free_ready.append((user.tg_id, sub.id))

        # paid → send now
        for tg_id, sub_id in paid_ready:
            await self._send(tg_id, text, _unsub_keyboard(sub_id))

        # free → schedule via Redis ZSET
        if free_ready:
            delay_ts = int(now.timestamp()) + self.settings.free_tier_notify_delay_sec
            payload = {
                "tg_ids_subs": free_ready,
                "text": text,
                "location_id": loc_id,
                "event_epoch": event_epoch,
            }
            await self.redis.zadd(
                DELAYED_ZSET, {orjson.dumps(payload).decode(): delay_ts}
            )

    # -------- delayed worker --------

    async def delayed_worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            now = int(datetime.now(timezone.utc).timestamp())
            due = await self.redis.zrangebyscore(DELAYED_ZSET, 0, now, start=0, num=100)
            for raw in due:
                # atomic "take": remove then process. If already taken by
                # another worker, zrem returns 0 and we skip.
                removed = await self.redis.zrem(DELAYED_ZSET, raw)
                if removed == 0:
                    continue
                try:
                    payload = orjson.loads(raw)
                except Exception:
                    continue

                # cancel if meanwhile the location became non-AVAILABLE
                cur_status = await self.redis.hget(STATUS_HASH, str(payload["location_id"]))
                if cur_status and cur_status != "AVAILABLE":
                    log.info(
                        "free_notify_cancelled",
                        location=payload["location_id"],
                        current_status=cur_status,
                    )
                    continue

                text = payload["text"]
                for tg_id, sub_id in payload["tg_ids_subs"]:
                    await self._send(tg_id, text, _unsub_keyboard(sub_id))

            try:
                await asyncio.wait_for(stop.wait(), timeout=5.0)
            except TimeoutError:
                pass

    # -------- main loop --------

    async def consume_events(self, stop: asyncio.Event) -> None:
        """XREAD loop — consume events into dispatch_event."""
        while not stop.is_set():
            try:
                resp = await self.redis.xread(
                    {EVENTS_STREAM: self._last_stream_id}, block=5_000, count=50
                )
            except Exception as e:  # noqa: BLE001
                log.warning("xread_failed", err=str(e))
                await asyncio.sleep(1)
                continue
            if not resp:
                continue
            for _, entries in resp:
                for msg_id, fields in entries:
                    self._last_stream_id = msg_id
                    try:
                        event = orjson.loads(fields["data"])
                    except Exception:
                        continue
                    try:
                        await self.dispatch_event(event)
                    except Exception as e:  # noqa: BLE001
                        log.exception("dispatch_failed", err=str(e))


async def tier_reaper(stop: asyncio.Event) -> None:
    """Periodically demote expired paid users to free tier in the DB.

    The notifier path already handles on-the-fly demotion, so this is mostly
    bookkeeping for `/status` and subscription-limit checks.
    """
    settings = get_settings()
    while not stop.is_set():
        now = datetime.now(timezone.utc)
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
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.tier_reaper_interval_sec)
        except TimeoutError:
            pass
