"""partial unique index on subscriptions (user_tg_id, location_id) WHERE mode='location'

Без этого индекса быстрый двойной клик «🔔 Подписаться» проходит мимо
SELECT-проверки в on_sub_callback и создаёт две одинаковые подписки.
Симптом для пользователя: дубль в /list и невозможность «отписаться» —
один /unsubscribe убирает только одну строку из двух.

Index partial — на mode='location'. NEAREST_* подписки поверх той же
локации legitimate (например, по геолокации в радиусе с другим
connector_type), их этот индекс не блокирует.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Если в проде уже накопились дубликаты, индекс не создастся.
    # Сворачиваем (user_tg_id, location_id) к минимальному id, остальные
    # удаляем — cascade подхватит их notification_log.
    op.execute(
        """
        DELETE FROM subscriptions
         WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       row_number() OVER (
                           PARTITION BY user_tg_id, location_id
                           ORDER BY id ASC
                       ) AS rn
                  FROM subscriptions
                 WHERE mode = 'location'
            ) t
            WHERE rn > 1
         )
        """
    )
    op.create_index(
        "uq_sub_user_location_locmode",
        "subscriptions",
        ["user_tg_id", "location_id"],
        unique=True,
        postgresql_where=sa.text("mode = 'location'"),
    )


def downgrade() -> None:
    op.drop_index("uq_sub_user_location_locmode", table_name="subscriptions")
