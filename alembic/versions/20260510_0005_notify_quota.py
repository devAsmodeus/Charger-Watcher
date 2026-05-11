"""subscriptions.notify_limit + notify_count

Юзер выбирает в wizard'е сколько раз хочет получить уведомление по
этой подписке: 1/2/3/5/10/∞. ``notify_limit`` хранит выбор (NULL = ∞).
``notify_count`` инкрементится в notifier-е после каждой успешной
доставки. По достижении лимита подписка удаляется и юзеру шлётся пуш
«квота исчерпана, подписаться снова».

Backfill для существующих подписок, созданных до wizard'а: limit=NULL
(∞), count=0. Это сохраняет текущий контракт «бесконечный спам» для
ранних подписчиков — следующая подписка будет идти через wizard.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-10
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("notify_limit", sa.Integer(), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "notify_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "notify_count")
    op.drop_column("subscriptions", "notify_limit")
