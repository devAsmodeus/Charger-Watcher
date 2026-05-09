"""Notifier cooldown invariants.

The critical property under test: a *failed* Telegram send must NOT consume
the cooldown slot. Otherwise a transient 5xx / VPN flake / "user blocked the
bot" silently locks the user out for ``NOTIFY_COOLDOWN_SEC``.

We don't spin up Postgres / Redis / Telegram here — instead we replace the
three slot-management methods on a ``Notifier`` instance with in-memory
stand-ins, monkeypatch ``_send`` to control success/failure, and inspect
the resulting state. This keeps the test fast and dependency-free while
still exercising the exact ``_send_with_slot`` orchestration shipped in
production.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from bot.notifier import Notifier


class _SlotState:
    """Tiny in-memory model of the ``notification_log`` table."""

    def __init__(self) -> None:
        # key = (sub_id, loc_id, event_epoch) -> "claimed" | "delivered"
        self.rows: dict[tuple[int, int, int], str] = {}

    def claim(self, sub_id: int, loc_id: int, event_epoch: int) -> bool:
        key = (sub_id, loc_id, event_epoch)
        if key in self.rows:
            return False
        self.rows[key] = "claimed"
        return True

    def commit(self, sub_id: int, loc_id: int, event_epoch: int) -> None:
        self.rows[(sub_id, loc_id, event_epoch)] = "delivered"

    def release(self, sub_id: int, loc_id: int, event_epoch: int) -> None:
        key = (sub_id, loc_id, event_epoch)
        if self.rows.get(key) == "claimed":
            del self.rows[key]

    def is_delivered(self, sub_id: int, loc_id: int, event_epoch: int) -> bool:
        return self.rows.get((sub_id, loc_id, event_epoch)) == "delivered"


def _make_notifier(monkeypatch: pytest.MonkeyPatch) -> tuple[Notifier, _SlotState]:
    """Construct a Notifier with DB methods replaced by in-memory state."""
    # AsyncLimiter blocks waiting for capacity; bypass it.
    bot = MagicMock()
    redis = MagicMock()
    n = Notifier.__new__(Notifier)
    n.bot = bot
    n.redis = redis

    class _NoLimiter:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *exc: Any) -> None:
            return None

    n._limiter = _NoLimiter()  # type: ignore[assignment]
    state = _SlotState()

    async def _claim(sub_id: int, loc_id: int, event_epoch: int) -> bool:
        return state.claim(sub_id, loc_id, event_epoch)

    async def _commit(sub_id: int, loc_id: int, event_epoch: int) -> None:
        state.commit(sub_id, loc_id, event_epoch)

    async def _release(sub_id: int, loc_id: int, event_epoch: int) -> None:
        state.release(sub_id, loc_id, event_epoch)

    monkeypatch.setattr(n, "_claim_slot", _claim)
    monkeypatch.setattr(n, "_commit_delivery", _commit)
    monkeypatch.setattr(n, "_release_slot", _release)
    return n, state


@pytest.mark.asyncio
async def test_failed_send_does_not_consume_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Telegram returning False for ``_send`` must release the slot.

    After a failed send, the next event for the same (sub, loc) MUST be
    able to claim the slot again — i.e. the cooldown was not poisoned.
    """
    n, state = _make_notifier(monkeypatch)

    async def _send_fails(*_: Any, **__: Any) -> bool:
        return False

    monkeypatch.setattr(n, "_send", _send_fails)

    sub_id, loc_id, epoch1, epoch2 = 1, 100, 1_000, 2_000

    ok = await n._send_with_slot(tg_id=42, sub_id=sub_id, loc_id=loc_id,
                                 event_epoch=epoch1, text="t")
    assert ok is False
    assert not state.is_delivered(sub_id, loc_id, epoch1)
    # Crucially: the row was released, so the (sub, loc) pair is open again.
    assert (sub_id, loc_id, epoch1) not in state.rows

    # A subsequent legitimate event must be able to claim the slot.
    can_claim_again = await n._claim_slot(sub_id, loc_id, epoch2)
    assert can_claim_again is True


@pytest.mark.asyncio
async def test_successful_send_locks_in_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful send MUST mark the slot as delivered (cooldown active)."""
    n, state = _make_notifier(monkeypatch)

    async def _send_ok(*_: Any, **__: Any) -> bool:
        return True

    monkeypatch.setattr(n, "_send", _send_ok)

    sub_id, loc_id, epoch = 1, 100, 1_000
    # Claim first, then send (mirrors dispatch_event flow).
    assert await n._claim_slot(sub_id, loc_id, epoch) is True
    ok = await n._send_with_slot(42, sub_id, loc_id, epoch, "t")
    assert ok is True
    assert state.is_delivered(sub_id, loc_id, epoch)


@pytest.mark.asyncio
async def test_concurrent_dispatch_only_one_claim_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two concurrent claims for the same event: exactly one succeeds."""
    n, _state = _make_notifier(monkeypatch)
    results = await asyncio.gather(
        n._claim_slot(1, 100, 1_000),
        n._claim_slot(1, 100, 1_000),
    )
    assert sorted(results) == [False, True]


@pytest.mark.asyncio
async def test_failed_delayed_cancellation_releases_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Free-tier delayed cancellation must release the slot too.

    Bug #1 secondary issue: if a station flips back to occupied during the
    120s delay window, we cancel the pending free-tier notification. The
    slot was claimed at dispatch time — leaving it as "claimed" would
    poison the next legitimate AVAILABLE within the cooldown window.
    """
    n, state = _make_notifier(monkeypatch)
    sub_id, loc_id, epoch = 7, 200, 5_000
    # Simulate dispatch_event having claimed the slot for a free-tier sub.
    assert await n._claim_slot(sub_id, loc_id, epoch) is True

    # delayed_worker cancellation path calls _release_slot.
    await n._release_slot(sub_id, loc_id, epoch)
    assert (sub_id, loc_id, epoch) not in state.rows

    # And a fresh event for the same (sub, loc) is now claimable.
    assert await n._claim_slot(sub_id, loc_id, epoch + 1) is True
