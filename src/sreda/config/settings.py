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

    # Chat LLM (Phase 3). Primary is MiMo-V2-Pro via its OpenAI-compatible
    # endpoint. ``mimo_api_key_file`` is a local filesystem path — useful
    # for dev: store the key in ``.secrets/mimo_api_key.txt`` (gitignored)
    # instead of shell-env. Precedence at resolve time:
    #   1. ``mimo_api_key`` (explicit)  → used as-is
    #   2. ``mimo_api_key_file``         → read from file
    #   3. ``None``                      → LLM features disabled
    mimo_base_url: str = "https://token-plan-sgp.xiaomimimo.com/v1"
    mimo_api_key: str | None = None
    mimo_api_key_file: str | None = None
    mimo_chat_model: str = "mimo-v2-pro"
    mimo_request_timeout_seconds: float = 60.0
    # Cheap-model hook for per-event relevance classification + future
    # ``decide_to_speak`` LLM layer. When set (e.g. ``mimo-v2-omni`` or
    # the yet-unreleased ``mimo-v2-flash``), the classifier worker starts
    # scoring inbound events that arrive without a skill-provided score.
    # None → classifier worker is disabled; skills must score their own
    # events via domain rules.
    mimo_classifier_model: str | None = None

    # Embeddings service (Phase 3). Separate from chat LLM so we can
    # point the two at different endpoints — common setup is MiMo for
    # chat + local LM Studio (multilingual-e5-large) for embeddings.
    # Leave ``embeddings_base_url`` = None to run with a deterministic
    # in-process fallback (tests / bootstrap mode).
    embeddings_base_url: str | None = None
    embeddings_api_key: str = "lm-studio"
    embeddings_model: str | None = None
    embeddings_request_timeout_seconds: float = 30.0

    connect_session_ttl_minutes: int = 15
    encryption_key: str | None = None
    encryption_key_id: str = "primary"
    encryption_key_salt: str | None = None
    encryption_legacy_keys: str | None = None  # JSON: {"key_id": "base64_or_hex_material"}
    feature_modules_raw: str | None = Field(default=None, validation_alias="SREDA_FEATURE_MODULES")

    # Admin dashboard token. When set, /admin/* routes are accessible
    # with ?token=<value>. When None, admin is disabled (403).
    admin_token: str | None = None

    # CSV list of log files surfaced in the /admin/logs view. Each entry
    # may be a plain path (``/tmp/sreda-uvicorn.log``) or ``label=path``
    # (``Uvicorn=/tmp/sreda-uvicorn.log``) for a friendlier nav label.
    # Defaults to the launchd plist StandardOut/StandardError paths used
    # on the Mac mini deploy. Files that don't exist are shown disabled
    # in the dropdown — they never become a 500 for the admin.
    # Default labels describe WHAT each service does so the dropdown is
    # readable without knowing the process name. Labels on the Mac mini
    # deploy:
    #   - Uvicorn       → serves Mini App, /connect/eds form, admin UI,
    #                     Telegram webhook. HTTP access log + app errors.
    #   - Long-poll     → pulls getUpdates from Telegram and forwards
    #                     each update to the local webhook (Mac is NAT'd).
    #                     If the bot goes silent — look here first.
    #   - Job runner    → background worker: EDS polling, credential
    #                     verification, subscription renewal, proactive
    #                     delivery.
    #   - pproxy        → HTTP-to-SOCKS5 shim for outbound traffic that
    #                     must exit through the VPS tunnel (e.g. EDS).
    admin_log_files_raw: str = Field(
        default=(
            "Веб-сервер (Mini App и админка)=/tmp/sreda-uvicorn.log,"
            "Приём сообщений из Telegram=/tmp/sreda-long-poll.log,"
            "Фоновые задачи (EDS-мониторинг и верификация)=/tmp/sreda-job-runner.log,"
            "Прокси исходящих запросов (HTTP→SOCKS5)=/tmp/sreda-pproxy.log"
        ),
        validation_alias="SREDA_ADMIN_LOG_FILES",
    )

    # Hard wall-clock budget for a single job run. Applied around the
    # network-bound parts of job handlers (Telegram send, EDS adapter
    # verification, etc.) so a hung upstream cannot pin a job in
    # ``running`` forever.
    job_max_runtime_seconds: float = 120.0

    # Polling interval for the always-on job runner (spec 36 Stage 2).
    # Set to 0 to disable the loop and only run a single pass (used in tests).
    job_poll_interval_seconds: float = 5.0

    # Speech recognition provider. Set to "yandex" to enable Yandex SpeechKit.
    # Leave None to disable voice transcription entirely.
    speech_provider: str | None = Field(default=None, validation_alias="SREDA_SPEECH_PROVIDER")
    yandex_speechkit_api_key: str | None = Field(
        default=None, validation_alias="SREDA_YANDEX_SPEECHKIT_API_KEY"
    )

    # Опциональный путь к файлу для structured JSON-лога неудачных
    # попыток подключения EDS-ЛК (для post-mortem анализа). Одна
    # запись — одна строка JSON с timestamp, tenant_id, login_masked,
    # error_code, underlying exception. Если None — лог в файл не
    # ведётся (данные всё равно попадают в обычный logger).
    failed_connect_log_path: str | None = None

    # Feature-request log: когда LLM-ассистент встречает запрос,
    # который он не может выполнить (не хватает скила, нет tool'а), он
    # зовёт tool ``log_unsupported_request`` → пишется строка в этот
    # файл. Помогает продукту видеть реальные хотелки пользователей и
    # выстраивать roadmap. Если None — строки уходят только в общий
    # logger (можно грепать по имени ``sreda.feature_requests``).
    feature_requests_log_path: str | None = None

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
    rate_limit_miniapp_max_requests: int = 60
    rate_limit_miniapp_window_seconds: float = 60.0

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

    @property
    def admin_log_files(self) -> list[tuple[str, str]]:
        """Parse ``admin_log_files_raw`` into ``[(label, path), ...]``.

        Entries without an explicit ``label=`` get the path's basename
        as the label. Empty or blank configuration returns ``[]``.
        """
        raw = self.admin_log_files_raw or ""
        result: list[tuple[str, str]] = []
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            if "=" in item:
                label, _, path = item.partition("=")
                label = label.strip() or path.strip().rsplit("/", 1)[-1]
                path = path.strip()
            else:
                path = item
                label = path.rsplit("/", 1)[-1]
            if path:
                result.append((label, path))
        return result

    def resolve_mimo_api_key(self) -> str | None:
        """Resolve MiMo API key with file-based fallback.

        Precedence: explicit env value → file contents → None. The file
        path is relative to the process CWD (typically the package root
        in dev, or wherever systemd runs the service in prod)."""
        if self.mimo_api_key:
            return self.mimo_api_key.strip()
        if self.mimo_api_key_file:
            from pathlib import Path

            path = Path(self.mimo_api_key_file)
            if path.exists() and path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                return value or None
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
