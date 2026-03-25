from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SREDA_", extra="ignore")

    app_name: str = "Sreda"
    env: str = "dev"
    log_level: str = "INFO"

    api_host: str = "127.0.0.1"
    api_port: int = 8000

    database_url: str = Field(default="postgresql+psycopg://sreda:sreda@localhost:5432/sreda")
    telegram_bot_token: str | None = None

    openai_base_url: str | None = None
    openai_api_key: str | None = None
    openai_model: str | None = None

    encryption_key: str | None = None
    feature_modules_raw: str | None = Field(default=None, validation_alias="SREDA_FEATURE_MODULES")

    @property
    def feature_modules(self) -> list[str]:
        raw = self.feature_modules_raw
        if not raw:
            return []
        return [item.strip() for item in raw.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
