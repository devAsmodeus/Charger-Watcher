"""unique dedupe constraint + cooldown index on notification_log

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-20

"""
from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop the non-unique dedupe index first (if present from 0001)
    op.drop_index("ix_notification_dedupe", table_name="notification_log")
    op.create_unique_constraint(
        "uq_notification_dedupe",
        "notification_log",
        ["subscription_id", "location_id", "event_epoch"],
    )
    op.create_index(
        "ix_notification_cooldown",
        "notification_log",
        ["subscription_id", "location_id", "sent_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_cooldown", table_name="notification_log")
    op.drop_constraint("uq_notification_dedupe", "notification_log", type_="unique")
    op.create_index(
        "ix_notification_dedupe",
        "notification_log",
        ["subscription_id", "location_id", "event_epoch"],
    )
