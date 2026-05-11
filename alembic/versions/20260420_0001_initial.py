"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-20

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("tg_id", sa.BigInteger(), primary_key=True),
        sa.Column("tier", sa.String(16), nullable=False, server_default="free"),
        sa.Column("paid_until", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "locations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("operator", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("address", sa.String(1024), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("last_status", sa.String(32)),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("operator", "external_id", name="uq_location_operator_external"),
    )
    op.create_index("ix_location_geo", "locations", ["latitude", "longitude"])

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_tg_id",
            sa.BigInteger(),
            sa.ForeignKey("users.tg_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column(
            "location_id",
            sa.BigInteger(),
            sa.ForeignKey("locations.id", ondelete="CASCADE"),
        ),
        sa.Column("anchor_latitude", sa.Float()),
        sa.Column("anchor_longitude", sa.Float()),
        sa.Column("radius_meters", sa.Integer()),
        sa.Column("connector_type", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_subscription_location", "subscriptions", ["location_id"])
    op.create_index("ix_subscription_user", "subscriptions", ["user_tg_id"])

    op.create_table(
        "notification_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "subscription_id",
            sa.BigInteger(),
            sa.ForeignKey("subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "location_id",
            sa.BigInteger(),
            sa.ForeignKey("locations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_epoch", sa.BigInteger(), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_notification_dedupe",
        "notification_log",
        ["subscription_id", "location_id", "event_epoch"],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_dedupe", table_name="notification_log")
    op.drop_table("notification_log")
    op.drop_index("ix_subscription_user", table_name="subscriptions")
    op.drop_index("ix_subscription_location", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_index("ix_location_geo", table_name="locations")
    op.drop_table("locations")
    op.drop_table("users")
