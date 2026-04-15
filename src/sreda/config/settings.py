from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_HOST_SUFFIXES = (".test", ".local", ".localhost")
_DEV_HOST_NAMES = {"localhost", "127.0.0.1", "::1"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SREDA_", extra="ignore")

    app_name: str = "Sreda"
    env: str = "dev"
    log_level: str = "INFO"

    api_host: str = "127.0.0.1"
    api_port: int = 8000

    database_url: str = Field(default="postgresql+psycopg://sreda:sreda@localhost:5432/sreda")
    telegram_bot_token: str | None = None
    telegram_webhook_secret_token: str | None = None
    connect_public_base_url: str | None = None

    openai_base_url: str | None = None
    openai_api_key: str | None = None
    openai_model: str | None = None

    connect_session_ttl_minutes: int = 15
    encryption_key: str | None = None
    encryption_key_id: str = "primary"
    encryption_key_salt: str | None = None
    encryption_legacy_keys: str | None = None  # JSON: {"key_id": "base64_or_hex_material"}
    feature_modules_raw: str | None = Field(default=None, validation_alias="SREDA_FEATURE_MODULES")

    # Hard wall-clock budget for a single job run. Applied around the
    # network-bound parts of job handlers (Telegram send, EDS adapter
    # verification, etc.) so a hung upstream cannot pin a job in
    # ``running`` forever.
    job_max_runtime_seconds: float = 120.0

    # Опциональный путь к файлу для structured JSON-лога неудачных
    # попыток подключения EDS-ЛК (для post-mortem анализа). Одна
    # запись — одна строка JSON с timestamp, tenant_id, login_masked,
    # error_code, underlying exception. Если None — лог в файл не
    # ведётся (данные всё равно попадают в обычный logger).
    failed_connect_log_path: str | None = None

    # Per-IP rate limits for public endpoints. These are defence-in-depth
    # on top of any reverse-proxy limiting that might be in front: they
    # guarantee a floor of protection for single-process deployments and
    # dev/debug runs where nginx is not in the picture.
    #
    # Set ``max_requests`` to 0 to reject all traffic (panic kill-switch)
    # or raise it high to effectively disable the limiter without
    # removing the Depends wiring.
    rate_limit_connect_max_requests: int = 20
    rate_limit_connect_window_seconds: float = 60.0
    rate_limit_telegram_max_requests: int = 120
    rate_limit_telegram_window_seconds: float = 60.0

    @field_validator("connect_public_base_url")
    @classmethod
    def _validate_connect_public_base_url(cls, value: str | None) -> str | None:
        # The public connect URL gets embedded into one-time links we
        # send to users in Telegram. A misconfigured scheme/host would
        # turn this into an open-redirect / phishing vector for EDS
        # credentials, so we fail fast at config load with strict rules:
        #
        #   * scheme must be http or https
        #   * plain http is only allowed for local/dev hosts
        #     (localhost, 127.0.0.1, ``*.test``, ``*.local``,
        #     ``*.localhost``) so production misconfig can't downgrade
        #     the link
        #   * host must be present (``https://`` alone is rejected)
        if value is None:
            return None
        candidate = value.strip()
        if not candidate:
            return None
        try:
            parsed = urlsplit(candidate)
        except ValueError as exc:
            raise ValueError(f"connect_public_base_url is not a valid URL: {value!r}") from exc
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(
                f"connect_public_base_url must use http or https, got {parsed.scheme!r}"
            )
        hostname = (parsed.hostname or "").lower()
        if not hostname:
            raise ValueError("connect_public_base_url is missing a hostname")
        if parsed.scheme == "http":
            is_dev_host = hostname in _DEV_HOST_NAMES or any(
                hostname == suffix.lstrip(".") or hostname.endswith(suffix)
                for suffix in _DEV_HOST_SUFFIXES
            )
            if not is_dev_host:
                raise ValueError(
                    "connect_public_base_url must use https for non-local hosts, "
                    f"got http://{hostname}"
                )
        return candidate

    @property
    def feature_modules(self) -> list[str]:
        raw = self.feature_modules_raw
        if not raw:
            return []
        return [item.strip() for item in raw.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
