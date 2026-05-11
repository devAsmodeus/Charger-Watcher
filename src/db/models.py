from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base

if TYPE_CHECKING:
    from datetime import datetime


class Tier(StrEnum):
    FREE = "free"
    PAID = "paid"


class SubscriptionMode(StrEnum):
    LOCATION = "location"           # follow exact location id
    NEAREST_GEO = "nearest_geo"     # radius from user-shared coordinates
    NEAREST_ADDRESS = "nearest_address"  # radius from geocoded address


class User(Base):
    __tablename__ = "users"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tier: Mapped[str] = mapped_column(String(16), default=Tier.FREE.value, nullable=False)
    paid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    subscriptions: Mapped[list[Subscription]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Location(Base):
    """Flat cache of all known locations across all operators."""

    __tablename__ = "locations"
    __table_args__ = (
        UniqueConstraint("operator", "external_id", name="uq_location_operator_external"),
        Index("ix_location_geo", "latitude", "longitude"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    operator: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    address: Mapped[str] = mapped_column(String(1024), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    # Last observed aggregated status. Nullable because Primary network omits it.
    last_status: Mapped[str | None] = mapped_column(String(32), default=None)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        Index("ix_subscription_location", "location_id"),
        Index("ix_subscription_user", "user_tg_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_tg_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.tg_id", ondelete="CASCADE"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    # For LOCATION mode — concrete location row in the cache.
    location_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("locations.id", ondelete="CASCADE"), default=None
    )
    # For NEAREST_* modes — search anchor + radius in meters.
    anchor_latitude: Mapped[float | None] = mapped_column(Float, default=None)
    anchor_longitude: Mapped[float | None] = mapped_column(Float, default=None)
    radius_meters: Mapped[int | None] = mapped_column(default=None)
    # Optional filter by connector type (human label from LocationDetail).
    # NULL = подписка на любой свободный коннектор.
    connector_type: Mapped[str | None] = mapped_column(String(64), default=None)
    # Сколько ещё уведомлений нужно отправить по этой подписке. NULL = ∞.
    # Декремент через инкремент notify_count в notifier-е после успешной
    # доставки; при notify_count >= notify_limit подписка удаляется.
    notify_limit: Mapped[int | None] = mapped_column(default=None)
    notify_count: Mapped[int] = mapped_column(
        default=0, server_default="0", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="subscriptions")
    location: Mapped[Location | None] = relationship()


class Payment(Base):
    """Журнал Stars-платежей. Хранит charge_id для Refund Policy.

    user_tg_id — nullable: после /delete_me ссылка на юзера обнуляется,
    но запись о платеже остаётся для бухгалтерии (legal).
    """

    __tablename__ = "payments"
    __table_args__ = (Index("ix_payments_user", "user_tg_id"),)

    charge_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_tg_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.tg_id", ondelete="SET NULL"),
        default=None,
    )
    amount_stars: Mapped[int] = mapped_column(nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    payload: Mapped[str] = mapped_column(String(64), nullable=False)
    paid_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    refunded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )


class NotificationLog(Base):
    """Dedupe + audit. One row per (subscription, location, status transition)."""

    __tablename__ = "notification_log"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id",
            "location_id",
            "event_epoch",
            name="uq_notification_dedupe",
        ),
        Index("ix_notification_cooldown", "subscription_id", "location_id", "sent_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subscription_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    location_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("locations.id", ondelete="CASCADE"), nullable=False
    )
    # Epoch of the event the notification was caused by. Identical across recipients
    # of the same event — used to dedupe retries.
    event_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Time the row was *claimed* (insert moment). Used to expire stale claims
    # whose delivery never completed (process crash between insert and send).
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # NULL until the Telegram send actually succeeded. Cooldown only honors
    # rows where delivered_at IS NOT NULL — a failed send does NOT poison the
    # cooldown window. See _can_notify / _commit_delivery in notifier.py.
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
