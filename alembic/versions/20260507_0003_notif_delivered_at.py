"""notification_log.delivered_at — split claim from delivery

Adds a nullable ``delivered_at`` timestamp on ``notification_log``.

Cooldown is now enforced only against rows where ``delivered_at IS NOT NULL``,
so a transient Telegram failure no longer poisons the cooldown window for
the affected (subscription, location) pair.

Backfill: existing rows are assumed to represent successful deliveries
(this matches the pre-fix invariant where rows were inserted right before
sending and never deleted), so we copy ``sent_at`` into ``delivered_at``
for them.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-07
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notification_log",
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Backfill: existing rows in the table were inserted under the old
    # "insert-then-send" code path. They might or might not have actually
    # been delivered, but treating them as delivered is the conservative
    # choice — it preserves the existing cooldown behavior for rows that
    # were already there before this migration ran.
    op.execute(
        "UPDATE notification_log SET delivered_at = sent_at WHERE delivered_at IS NULL"
    )
    # Index on delivered_at to keep the cooldown lookup cheap (the existing
    # ix_notification_cooldown is on sent_at and is no longer the right
    # column for cooldown checks).
    op.create_index(
        "ix_notification_delivered_cooldown",
        "notification_log",
        ["subscription_id", "location_id", "delivered_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_delivered_cooldown", table_name="notification_log"
    )
    op.drop_column("notification_log", "delivered_at")
