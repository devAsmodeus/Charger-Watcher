"""referrals — таблица «кто пригласил кого»

Юзер, попавший в бота по deep-link `/start ref_<inviter_tg_id>`, получает
скидку на первый платёж (100 ⭐ вместо 150). После оплаты инвайтер
получает +15 дней paid. Refund инвайти возвращает -15 дней инвайтеру.

invitee_tg_id — PK: один инвайт на инвайти, повторно его не «продать».

ON DELETE CASCADE на обоих FK — /delete_me юзера сносит его реферальные
связи (как инвайтера, так и инвайти).

invitee_charge_id — payments(charge_id), nullable: NULL пока инвайти не
оплатил. Заполняется в on_paid после успешной оплаты.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-12
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "referrals",
        sa.Column(
            "invitee_tg_id",
            sa.BigInteger(),
            sa.ForeignKey("users.tg_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "inviter_tg_id",
            sa.BigInteger(),
            sa.ForeignKey("users.tg_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("rewarded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "invitee_charge_id",
            sa.String(128),
            sa.ForeignKey("payments.charge_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "inviter_tg_id <> invitee_tg_id", name="ck_referrals_no_self"
        ),
    )
    op.create_index("ix_referrals_inviter", "referrals", ["inviter_tg_id"])
    op.create_index(
        "ix_referrals_charge", "referrals", ["invitee_charge_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_referrals_charge", table_name="referrals")
    op.drop_index("ix_referrals_inviter", table_name="referrals")
    op.drop_table("referrals")
