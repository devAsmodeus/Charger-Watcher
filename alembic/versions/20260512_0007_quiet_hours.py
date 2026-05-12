"""quiet hours per user — quiet_from, quiet_to, tz

Юзер задаёт окно «не беспокоить» (например, 22:00-08:00). Notifier перед
send_message проверяет текущее время в часовом поясе юзера и, если оно
внутри окна, кладёт уведомление в delayed-queue с deliver_at = next
открытое окно (выход из quiet hours).

Оба поля NULL = тихих часов нет (поведение «как раньше»). Если задано
только одно — миграция отвергнет, но в коде это не репрезентативно;
для простоты лечим на уровне UI (set всегда парно).

tz хранится строкой IANA (например 'Europe/Minsk'). NOT NULL с дефолтом —
бот в БРБ-сегменте, дефолт логичен.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-12
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("quiet_from", sa.Time(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("quiet_to", sa.Time(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "tz",
            sa.String(64),
            nullable=False,
            server_default="Europe/Minsk",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "tz")
    op.drop_column("users", "quiet_to")
    op.drop_column("users", "quiet_from")
