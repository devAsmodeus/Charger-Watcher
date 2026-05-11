"""payments — журнал Telegram Stars-транзакций

Без сохранения ``telegram_payment_charge_id`` команда ``/refund`` (обещана
в ``docs/legal/refund_policy.md``) физически невозможна — Telegram
``payments.refundStarsCharge`` принимает (user_id, charge_id), и второй
аргумент мы пока теряем сразу после ``successful_payment``.

ON DELETE SET NULL для ``user_tg_id``: ``/delete_me`` обнуляет ссылку,
сами строки остаются для бухгалтерии (ToS обещает хранить платёжные
записи столько, сколько требует закон).

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("charge_id", sa.String(128), primary_key=True),
        sa.Column(
            "user_tg_id",
            sa.BigInteger(),
            sa.ForeignKey("users.tg_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("amount_stars", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("payload", sa.String(64), nullable=False),
        sa.Column(
            "paid_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_payments_user", "payments", ["user_tg_id"])


def downgrade() -> None:
    op.drop_index("ix_payments_user", table_name="payments")
    op.drop_table("payments")
