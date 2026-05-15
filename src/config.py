from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    tg_bot_token: str = Field(default="", alias="TG_BOT_TOKEN")

    database_url: str = Field(
        default="postgresql+asyncpg://charger:charger@localhost:5432/charger",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    api_base: str = Field(default="https://api.example.com", alias="API_BASE")
    api_origin: str = Field(default="", alias="API_ORIGIN")
    api_user_agent: str = Field(
        default="charger-watcher/0.1 (+https://github.com/devAsmodeus/Charger-Watcher)",
        alias="API_USER_AGENT",
    )
    poll_interval_sec: int = Field(default=10, alias="POLL_INTERVAL_SEC")
    catalog_sync_interval_sec: int = Field(default=300, alias="CATALOG_SYNC_INTERVAL_SEC")
    sse_sync_interval_sec: int = Field(default=15, alias="SSE_SYNC_INTERVAL_SEC")
    # Раз в N сек poller обходит весь каталог и обновляет Redis-кэш
    # location_connectors:<id> — статичные типы коннекторов на каждой
    # станции. Bot читает этот кэш в wizard'е подписки. Типы меняются
    # редко (физическое железо), 6 ч — с запасом.
    connectors_sync_interval_sec: int = Field(
        default=21_600, alias="CONNECTORS_SYNC_INTERVAL_SEC"
    )
    http_timeout_sec: int = Field(default=15, alias="HTTP_TIMEOUT_SEC")
    http_proxy_url: str | None = Field(default=None, alias="HTTP_PROXY_URL")

    free_tier_notify_delay_sec: int = Field(default=120, alias="FREE_TIER_NOTIFY_DELAY_SEC")
    free_tier_max_subscriptions: int = Field(default=1, alias="FREE_TIER_MAX_SUBSCRIPTIONS")
    paid_tier_max_subscriptions: int = Field(default=5, alias="PAID_TIER_MAX_SUBSCRIPTIONS")
    # Cooldown между двумя успешными push'ами по одной (sub, loc) — защита
    # от flicker'а станции (Available→Busy→Available за несколько секунд).
    # Раньше было 600s = 10 мин — съедало легитимные транзишены вида
    # «занято 4 минуты, отъехал, снова свободно» как silent loss. 60s
    # достаточно против flicker'а и не мешает реальным циклам.
    notify_cooldown_sec: int = Field(default=60, alias="NOTIFY_COOLDOWN_SEC")
    tier_reaper_interval_sec: int = Field(default=3600, alias="TIER_REAPER_INTERVAL_SEC")
    tg_send_rate_per_sec: int = Field(default=20, alias="TG_SEND_RATE_PER_SEC")

    paid_tier_price_stars: int = Field(default=150, alias="PAID_TIER_PRICE_STARS")
    paid_tier_duration_days: int = Field(default=30, alias="PAID_TIER_DURATION_DAYS")

    # Реферальная программа.
    # Инвайти платит за первый paid `referral_invitee_price_stars` ⭐ (скидка).
    # Инвайтер получает +`referral_reward_days` дней paid после оплаты.
    referral_invitee_price_stars: int = Field(default=100, alias="REFERRAL_INVITEE_PRICE_STARS")
    referral_reward_days: int = Field(default=15, alias="REFERRAL_REWARD_DAYS")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # CSV of Telegram user ids with admin rights (/stats and future admin cmds).
    # Список парсим лениво в admin_ids_set() — pydantic не любит list[int] из env.
    admin_ids: str = Field(default="", alias="ADMIN_IDS")

    def admin_ids_set(self) -> set[int]:
        return {int(x) for x in self.admin_ids.split(",") if x.strip().isdigit()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
