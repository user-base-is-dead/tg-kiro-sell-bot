from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Fails fast on missing/invalid env vars — never falls back to a silent default for secrets."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(alias="BOT_TOKEN")
    admin_ids_raw: str = Field(default="", alias="ADMIN_IDS")

    database_url: str = Field(alias="DATABASE_URL")
    redis_url: str = Field(alias="REDIS_URL")

    support_group_id: int | None = Field(default=None, alias="SUPPORT_GROUP_ID")
    log_chat_id: int | None = Field(default=None, alias="LOG_CHAT_ID")

    default_currency: str = Field(default="USD", alias="DEFAULT_CURRENCY")
    default_locale: str = Field(default="en", alias="DEFAULT_LOCALE")

    encryption_key: str = Field(alias="ENCRYPTION_KEY")
    crypto_webhook_secret: str = Field(default="dev_secret", alias="CRYPTO_WEBHOOK_SECRET")
    wallet_address: str = Field(alias="WALLET_ADDRESS")
    bscscan_api_key: str = Field(alias="BSCSCAN_API_KEY")

    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("support_group_id", "log_chat_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v

    @property
    def admin_ids(self) -> list[int]:
        return [int(x) for x in self.admin_ids_raw.split(",") if x.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
