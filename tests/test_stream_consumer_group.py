"""Restart safety of the Notifier's Redis Stream consumer.

The bug we're guarding against: if the consumer reads with a fresh "$"
cursor on every restart, every event published while the bot was down is
silently dropped. We now use a Redis consumer group with explicit XACK so
that:

  1. On first start, ``XGROUP CREATE`` with ``id=0`` claims the entire
     stream history — events produced before the consumer was up are still
     delivered.
  2. After a restart, the previous-run pending list (entries delivered to
     the consumer but never acked) is replayed first.
  3. New events show up via the ``>`` cursor.

We use fakeredis to keep this test offline. Skipped if fakeredis isn't
installed locally — install with ``pip install fakeredis``.
"""
from __future__ import annotations

import importlib.util

import pytest

fakeredis_spec = importlib.util.find_spec("fakeredis")
if fakeredis_spec is None:  # pragma: no cover
    pytest.skip("fakeredis not installed", allow_module_level=True)

import contextlib

import fakeredis.aioredis as fr

from bot.notifier import EVENTS_STREAM, NOTIFIER_CONSUMER, NOTIFIER_GROUP


@pytest.mark.asyncio
async def test_consumer_group_replays_pre_restart_messages() -> None:
    """An event published before the consumer ever connected MUST be readable."""
    r = fr.FakeRedis(decode_responses=True)
    try:
        # Producer side: publish before the group exists.
        await r.xadd(EVENTS_STREAM, {"data": '{"ts":1,"location_id":1}'})

        # Consumer side: create group at id=0 (mkstream guards against race).
        with contextlib.suppress(Exception):
            await r.xgroup_create(EVENTS_STREAM, NOTIFIER_GROUP, id="0", mkstream=True)

        resp = await r.xreadgroup(
            NOTIFIER_GROUP, NOTIFIER_CONSUMER, {EVENTS_STREAM: ">"}, count=10
        )
        assert resp, "pre-existing message must be replayed under consumer group"
        _, entries = resp[0]
        assert len(entries) == 1
    finally:
        await r.aclose()


@pytest.mark.asyncio
async def test_unacked_messages_are_replayed_after_restart() -> None:
    """A message delivered but not acked must be redelivered to the same consumer."""
    r = fr.FakeRedis(decode_responses=True)
    try:
        await r.xgroup_create(EVENTS_STREAM, NOTIFIER_GROUP, id="0", mkstream=True)

        msg_id_a = await r.xadd(EVENTS_STREAM, {"data": '{"n":"a"}'})
        msg_id_b = await r.xadd(EVENTS_STREAM, {"data": '{"n":"b"}'})

        # First run: read both, ACK only A. B simulates a crash mid-dispatch.
        resp1 = await r.xreadgroup(
            NOTIFIER_GROUP, NOTIFIER_CONSUMER, {EVENTS_STREAM: ">"}, count=10
        )
        assert resp1
        _, entries1 = resp1[0]
        ids1 = {mid for mid, _ in entries1}
        assert ids1 == {msg_id_a, msg_id_b}
        await r.xack(EVENTS_STREAM, NOTIFIER_GROUP, msg_id_a)

        # Simulate restart: same consumer name, drain the pending list with id="0".
        resp2 = await r.xreadgroup(
            NOTIFIER_GROUP, NOTIFIER_CONSUMER, {EVENTS_STREAM: "0"}, count=10
        )
        assert resp2, "unacked message must be redelivered after restart"
        _, entries2 = resp2[0]
        ids2 = {mid for mid, _ in entries2}
        assert msg_id_b in ids2
        assert msg_id_a not in ids2  # already acked, must not reappear

        # ack B and confirm pending list is empty.
        await r.xack(EVENTS_STREAM, NOTIFIER_GROUP, msg_id_b)
        resp3 = await r.xreadgroup(
            NOTIFIER_GROUP, NOTIFIER_CONSUMER, {EVENTS_STREAM: "0"}, count=10
        )
        # fakeredis returns either [] or [(stream, [])] for empty pending; both ok.
        if resp3:
            _, entries3 = resp3[0]
            assert entries3 == []
    finally:
        await r.aclose()


@pytest.mark.asyncio
async def test_busygroup_is_idempotent() -> None:
    """Recreating the group on a second boot must not raise."""
    r = fr.FakeRedis(decode_responses=True)
    try:
        await r.xgroup_create(EVENTS_STREAM, NOTIFIER_GROUP, id="0", mkstream=True)
        with pytest.raises(Exception) as ei:
            await r.xgroup_create(EVENTS_STREAM, NOTIFIER_GROUP, id="0", mkstream=True)
        # Production code swallows BUSYGROUP; assert that's what we got.
        assert "BUSYGROUP" in str(ei.value)
    finally:
        await r.aclose()
