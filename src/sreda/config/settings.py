from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_HOST_SUFFIXES = (".test", ".local", ".localhost")
_DEV_HOST_NAMES = {"localhost", "127.0.0.1", "::1"}


# Имена полей, значения которых — СЕКРЕТЫ. При repr/str объекта Settings
# (например `print(settings)`, `f"settings={settings}"`, traceback с
# locals()) их значения заменяются на ``***``. Это защита от случайной
# утечки в логи / стек-трейсы / отладочный вывод.
#
# Field-тип остаётся ``str | None`` (а не ``SecretStr``) чтобы caller'ы
# работали как раньше: ``settings.mimo_api_key.strip()``,
# ``if settings.admin_token: ...`` — без ``.get_secret_value()``.
# Маскирование — только в repr-слое.
_SECRET_FIELD_NAMES = frozenset({
    "telegram_bot_token",
    "telegram_webhook_secret_token",
    "max_bot_token",
    "max_webhook_secret_token",
    "openai_api_key",
    "mimo_api_key",
    "openrouter_api_key",
    "inception_api_key",
    "embeddings_api_key",
    "encryption_key",
    "encryption_key_salt",
    "encryption_legacy_keys",
    "tg_account_salt",
    "admin_token",
    "yandex_speechkit_api_key",
    "groq_api_key",
    "tavily_api_key",
    # Phase 1: second Telegram bot (sreda_home) token
    "home_bot_token",
    # database_url содержит пароль PG в DSN — тоже маскируем
    "database_url",
    # #138 Р5: раздельные роль-DSN тоже несут пароль — маскируем
    "migration_database_url",
    "maintenance_database_url",
    "identity_database_url",
})


class _ReactTenantGate(frozenset):
    """Множество tenant-id для ReAct-гейтов с режимом «всем» (#159-rollout).

    Построено из env-значения ``*`` → ``__contains__`` истинно для ЛЮБОГО
    tenant_id (включая будущие регистрации), и объект truthy. Иначе ведёт себя
    как обычный ``frozenset`` (явный allowlist).

    Зачем подкласс, а не отдельный bool-флаг: гейт проверяется как ``tenant_id in
    gate`` в нескольких местах (telegram/max inbound + voice-gate) — подкласс
    сохраняет ВСЕ существующие call-site без правок (нулевой риск пропустить точку
    проверки + добавить новый дрейф). Применяется ТОЛЬКО к loop/prune гейтам;
    privacy-allowlist'ы (debug, admin preview) и эксперимент osa остаются строгими
    списками без «*». В режиме «всем» итерация/len дают ПУСТО (полный список
    тенантов не перечисляем — его и нет), поэтому проверяй принадлежность через
    ``in``, а наличие гейта — через ``bool``/``if``, не через ``len``."""

    def __new__(cls, items=(), *, all_: bool = False):
        self = super().__new__(cls, items)
        self._all = all_
        return self

    def __contains__(self, item: object) -> bool:
        if getattr(self, "_all", False):
            # Режим «всем»: членом считается ЛЮБОЙ непустой строковый tenant_id.
            # None/""/нестрока → False (анти-регресс: малформный id из call-site
            # ДО резолва тенанта не должен «тихо включиться» под `*`). Codex R1 MINOR.
            return isinstance(item, str) and bool(item)
        return super().__contains__(item)

    def __bool__(self) -> bool:
        return True if getattr(self, "_all", False) else len(self) > 0


def _parse_tenant_gate(raw: str | None) -> _ReactTenantGate:
    """CSV tenant-id → гейт. Одиночный ``*`` → режим «всем»; пусто/None → никому.
    Общий разбор для loop/prune гейтов (#159-rollout)."""
    if not raw:
        return _ReactTenantGate()
    if raw.strip() == "*":
        return _ReactTenantGate(all_=True)
    return _ReactTenantGate(item.strip() for item in raw.split(",") if item.strip())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SREDA_", extra="ignore")

    app_name: str = "Sreda"
    env: str = "dev"
    log_level: str = "INFO"

    api_host: str = "127.0.0.1"
    api_port: int = 8000

    database_url: str = Field(default="postgresql+psycopg://sreda:sreda@localhost:5432/sreda")
    # #138 Р5: раздельные DSN под роль-сплит. Пусто (None) → фолбэк на database_url
    # (аддитивно v1: пока роли не разведены на проде — все ходят под одной ролью, как
    # сегодня). Ф5 проставляет их (ops, SERVERS.md, не в репо):
    #   database_url            = app-DSN (рантайм под sreda_app);
    #   migration_database_url  = owner-DSN (ТОЛЬКО env.py — миграции обходят RLS);
    #   maintenance_database_url= sreda_maintenance (админ/GC/монитор в privileged-блоках);
    #   identity_database_url   = sreda_identity (inbound-провижн в privileged-блоках).
    migration_database_url: str | None = Field(default=None)
    maintenance_database_url: str | None = Field(default=None)
    identity_database_url: str | None = Field(default=None)
    # SQLAlchemy connection-pool sizing (PostgreSQL only; SQLite uses its own
    # pool). Defaults are intentionally SMALL: the background processes
    # (pollers, job-runner) are low-concurrency (~3-4 pooled conns each; the
    # poller's advisory lock uses a separate NullPool conn), so a 3+7 pool
    # leaves PG's 100-connection budget free for the API. The uvicorn process
    # RAISES these via env (SREDA_DB_POOL_SIZE / SREDA_DB_MAX_OVERFLOW in its
    # systemd unit) to cover its sync-handler threadpool concurrency under Mini
    # App open-bursts. Keep the total across all processes within PG
    # max_connections (minus superuser_reserved + the advisory-lock conns).
    # NB: the capacity budget assumes a SINGLE uvicorn worker (ExecStart has no
    # --workers). With N workers, uvicorn's pool multiplies by N — re-check the
    # PG budget before adding workers. le= caps stop an env typo
    # (e.g. SREDA_DB_MAX_OVERFLOW=300) from silently exhausting PG.
    db_pool_size: int = Field(default=3, ge=1, le=50)
    db_max_overflow: int = Field(default=7, ge=0, le=100)
    db_pool_timeout: int = Field(default=30, ge=1, le=120)
    db_pool_pre_ping: bool = True
    telegram_bot_token: str | None = None
    telegram_webhook_secret_token: str | None = None
    telegram_bot_username: str | None = None
    telegram_miniapp_shortname: str | None = None

    # --- Second Telegram bot: @sreda_home_bot (Phase 1) -----------------
    # Env names (env_prefix="SREDA_"): field home_bot_token → SREDA_HOME_BOT_TOKEN
    # etc.  Using plain field names (no alias) so pydantic-settings applies
    # the prefix automatically, giving the EXACT names:
    #   SREDA_HOME_BOT_TOKEN / SREDA_HOME_BOT_USERNAME / SREDA_HOME_MINIAPP_SHORTNAME
    # (NOT "SREDA_SREDA_HOME_BOT_TOKEN" which would happen with a field named
    # "sreda_home_bot_token").
    home_bot_token: str | None = None
    home_bot_username: str | None = None
    home_miniapp_shortname: str | None = None
    # When True the sreda_home bot auto-approves new users (no tenant moderation).
    # anti-abuse (rate-limit, capacity) still applies.  Default True = open signup.
    home_bot_signup_open: bool = True

    # --- Bot-key routing constants (env-driven; default = legacy "sreda") ----
    # system_default_bot_key: bot used for system broadcasts + outbox rows with
    #   no explicit bot_key during the migration window.
    # admin_bot_key: bot used for admin alerts to the operator.
    # Both default to "sreda" so existing behaviour is unchanged until an
    # operator explicitly sets them via env.
    system_default_bot_key: str = "sreda"
    admin_bot_key: str = "sreda"

    # MAX (российский мессенджер) channel — Phase 5 of MAX integration
    # sprint, 2026-05-04. ``max_bot_token`` доступен через
    # SREDA_MAX_BOT_TOKEN. ``max_webhook_url`` указывает на
    # https://bot.sredaspace.ru/api/max/webhook (lifespan auto-register'ит
    # на старте). Все три = None → MAX-код dormant (rollback path).
    max_bot_token: str | None = None
    max_webhook_url: str | None = None
    max_webhook_secret_token: str | None = None
    # #214: базовый URL MAX Bot API. МАКС мигрирует с platform-api.max.ru на
    # platform-api2.max.ru (до 19.07.2026; новый адрес отдаёт TLS, выпущенный
    # Минцифры — см. integrations/max/client.max_ssl_context). Дефолт = новый
    # адрес; откат на старый — сменой env SREDA_MAX_API_BASE_URL без редеплоя.
    max_api_base_url: str = Field(
        default="https://platform-api2.max.ru",
        validation_alias="SREDA_MAX_API_BASE_URL",
    )
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
    # Inception (api.inceptionlabs.ai) — OpenAI-compatible. Прямой провайдер
    # Mercury-2 (диффузионная LLM, 1000+ tps, cached input, tool-use). Ключ:
    # env → file → None, как у mimo. Переезд планировщика с MiMo (#60).
    inception_base_url: str = "https://api.inceptionlabs.ai/v1"
    inception_api_key: str | None = None
    inception_api_key_file: str | None = None
    # Cheap-model hook for per-event relevance classification + future
    # ``decide_to_speak`` LLM layer. When set (e.g. ``mimo-v2-omni`` or
    # the yet-unreleased ``mimo-v2-flash``), the classifier worker starts
    # scoring inbound events that arrive without a skill-provided score.
    # None → classifier worker is disabled; skills must score their own
    # events via domain rules.
    mimo_classifier_model: str | None = None

    # Alternative chat-LLM providers reachable via their OpenAI-compatible
    # endpoints. Selected at runtime through ``chat_provider`` below; the
    # defaults keep MiMo as the production brain so enabling openrouter
    # is an explicit, reviewable change (not accidentally triggered by
    # a forgotten env var).
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_api_key: str | None = None
    openrouter_api_key_file: str | None = None
    # Recommended defaults for the two front-runners from the 2026-04-22
    # bench. google/gemma-4-26b-a4b-it wins latency but emits a
    # ``thought\\n`` prefix we strip in services.llm. qwen3.6-plus is
    # the clean runner-up.
    openrouter_chat_model: str = "google/gemma-4-26b-a4b-it"

    # Chat-LLM dispatcher. Accepted values: ``mimo``, ``openrouter``.
    # ``chat_fallback_provider`` is optional — when set, the tool-loop
    # wraps the primary LLM with LangChain's ``.with_fallbacks([...])``
    # so a provider-level error (429/5xx/timeout) on any iteration
    # transparently retries the same step on the fallback provider
    # without losing the accumulated message history.
    chat_provider: str = Field(
        default="mimo",
        validation_alias=AliasChoices("chat_provider", "SREDA_CHAT_PROVIDER"),
    )
    chat_fallback_provider: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "chat_fallback_provider", "SREDA_CHAT_FALLBACK_PROVIDER"
        ),
    )

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
    # Salt для HMAC-SHA256 хеширования telegram chat_id (152-ФЗ
    # обезличивание Часть 1, 2026-04-27). Без этого salt'а lookup
    # по hash не работает — bot не найдёт юзера. Salt — secret-уровня:
    # никогда не менять после первого деплоя без full backfill.
    # Генерится `python -c 'import secrets; print(secrets.token_hex(32))'`.
    # ENV: SREDA_TG_ACCOUNT_SALT.
    tg_account_salt: str | None = None
    feature_modules_raw: str | None = Field(default=None, validation_alias="SREDA_FEATURE_MODULES")

    # Admin dashboard token. When set, /admin/* routes are accessible
    # with ?token=<value>. When None, admin is disabled (403).
    admin_token: str | None = None

    # Telegram chat_id для критических нотификаций админу (Boris).
    # Используется когда инстанс должен сообщить о serious issue:
    # embeddings не настроены, alembic head ≠ DB head, etc. Через этот
    # канал бот шлёт сообщения вида «🔴 startup: ... недоступен».
    #
    # Default = None: альерты только в логи, в Telegram ничего не идёт.
    # Для prod-инстанса (Boris's чат с ботом) выставить
    # SREDA_ADMIN_TELEGRAM_CHAT_ID=352612382 в .env. Hardcode'ить здесь
    # 352612382 нельзя — любой OSS-deploy без явной настройки молча
    # отправлял бы alert'ы в Boris'овский чат вместо своего.
    admin_telegram_chat_id: str | None = None

    # R-28 amendment 2026-05-15: админ-алерты переехали с TG на MAX как
    # primary channel (Boris directive «пусть прилетает в макс а не в
    # телегу»). TG остаётся fallback'ом если MAX POST упал (HTTP non-2xx
    # или timeout) — двойная защита от silent loss.
    #
    # Default = None: MAX-канал не используется, fall-through на
    # admin_telegram_chat_id (legacy path). Для prod выставить
    # SREDA_ADMIN_MAX_CHAT_ID=320955459 (Boris's MAX chat_id).
    #
    # Требует ``max_bot_token`` (см. выше) — без него MAX disabled
    # и алерт автоматически идёт в TG. Hardcode 320955459 нельзя — те же
    # OSS-soundsafety причины что и admin_telegram_chat_id.
    admin_max_chat_id: str | None = None

    # #305: allowlist tg id (через запятую) для входа в админку через Telegram.
    # Пусто → Telegram-вход выключен, fallback-токен работает. Raw-строка +
    # property (pydantic list из env ждёт JSON, поэтому CSV парсим сами).
    admin_tg_ids_raw: str | None = Field(default=None, validation_alias="SREDA_ADMIN_TG_IDS")

    @property
    def admin_tg_ids(self) -> frozenset[str]:
        if not self.admin_tg_ids_raw:
            return frozenset()
        return frozenset(x.strip() for x in self.admin_tg_ids_raw.split(",") if x.strip())

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

    # #292: интервал фонового пересбора снапшота обзорного дашборда
    # (job_runner). Страница /admin только читает снапшот; 0 или меньше —
    # луп выключен (дев-машины без нужды в фоне).
    admin_dashboard_refresh_seconds: float = Field(
        default=300.0,
        validation_alias="SREDA_ADMIN_DASHBOARD_REFRESH_SECONDS",
    )

    # Hard wall-clock budget for a single job run. Applied around the
    # network-bound parts of job handlers (Telegram send, EDS adapter
    # verification, etc.) so a hung upstream cannot pin a job in
    # ``running`` forever.
    # 2026-04-28: bumped 120 → 240 to accomodate slow MiMo turns where
    # LLM may take 100+s on prompt 21k + completion 400+. Inner
    # CHAT_TURN_TIMEOUT_SECONDS = 180 — outer must be larger so the
    # handler-level timeout (with tool-summary rescue) fires before
    # this one (which produces a generic "отменено по таймауту").
    job_max_runtime_seconds: float = 240.0

    # Worker loop tick. Default 1s — alarm-style UX for reminders needs
    # worst-case latency under ~1s (user-perceivable jitter). Cost is a
    # handful of empty SQL queries per second across workers; trivial on
    # SQLite and Postgres. Set to 0 to disable the loop (tests only).
    job_poll_interval_seconds: float = 1.0

    # Ack edit UX kill-switches. When disabled, inbound/delivery code
    # keeps the legacy flow: send quick ack, send final as a separate
    # message, then clean up the ack where supported.
    ack_edit_telegram_enabled: bool = Field(
        default=True, validation_alias="SREDA_ACK_EDIT_TELEGRAM_ENABLED"
    )
    ack_edit_max_enabled: bool = Field(
        default=True, validation_alias="SREDA_ACK_EDIT_MAX_ENABLED"
    )
    ack_streaming_enabled: bool = Field(
        default=True, validation_alias="SREDA_ACK_STREAMING_ENABLED"
    )
    ack_streaming_min_interval_seconds: float = Field(
        default=0.35, validation_alias="SREDA_ACK_STREAMING_MIN_INTERVAL_SECONDS"
    )

    # Issue #68 — Full LLM trace logging (request + response + tool_calls)
    # на диск /var/lib/sreda/private/llm-traces/YYYY-MM-DD/{trace_id}.jsonl
    # для post-mortem debug и replay в другую LLM. Содержит decrypted PII
    # (memories, user text, tool args) → 152-ФЗ-sensitive. Default off;
    # включается явно в prod .env.
    #
    # Plan: plans/mellow-discovering-conway-final.md
    llm_trace_logging_enabled: bool = Field(
        default=False, validation_alias="SREDA_LLM_TRACE_LOGGING_ENABLED"
    )
    # Compliance-strict mode: если =True, бот **не звонит LLM** когда trace
    # не удалось записать. Гарантирует что нет «секретного» обращения без
    # аудита. Default fail-open для production stability — request degraded
    # → admin alert P1 но turn продолжается.
    llm_trace_require_persist: bool = Field(
        default=False, validation_alias="SREDA_LLM_TRACE_REQUIRE_PERSIST"
    )

    # Plan-execute rollout — two independent per-tenant whitelists
    # (Hakuoro357/vex-assistant#74). Default empty → everyone stays on
    # the legacy inline path; existing behaviour is unchanged until a
    # tenant id appears in the relevant list.
    #
    # ``SREDA_MESSAGE_QUEUE_ENABLED_TENANTS`` — tenants whose inbound
    # messages go through the new ``message_jobs`` per-thread FIFO
    # queue instead of the inline ``handlers.chat()`` path. Enables
    # observability + lease-fencing without touching the planner.
    #
    # ``SREDA_PLANNER_ENABLED_TENANTS`` — tenants whose worker-side
    # processing uses the new planner-flow (planner → executor →
    # composer). Has effect only when the same tenant is also in the
    # queue whitelist (planner-flow lives inside the worker).
    #
    # Rollout pattern: enable queue first for Boris alone, then expand
    # to test users → all. Planner stays on Boris until graph edge
    # lands in Sub-A6.
    message_queue_enabled_tenants_raw: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SREDA_MESSAGE_QUEUE_ENABLED_TENANTS",
            "sreda_message_queue_enabled_tenants",
        ),
    )
    planner_mode_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "SREDA_PLANNER_MODE_ENABLED",
            "sreda_planner_mode_enabled",
        ),
    )
    # #135: гейт памяти планов (сбор+тень). Пусто = выключено для всех.
    plan_library_enabled_tenants_raw: str | None = Field(
        default=None,
        validation_alias="SREDA_PLAN_LIBRARY_ENABLED_TENANTS",
    )
    planner_enabled_tenants_raw: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SREDA_PLANNER_ENABLED_TENANTS",
            "sreda_planner_enabled_tenants",
        ),
    )
    react_loop_enabled_tenants_raw: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SREDA_REACT_LOOP_ENABLED_TENANTS",
            "sreda_react_loop_enabled_tenants",
        ),
        description=(
            "#66 gated experiment: tenants routed to the new LangGraph "
            "ReAct+interrupt conversational loop (InMemory checkpointer, no "
            "PII at rest). Empty (default) → nobody; everyone stays on the "
            "existing inline path (zero regression)."
        ),
    )
    # #173/#184: тенанты, чей ReAct-цикл крутится на «Осе» (gpt-oss-120b @ Groq) ВМЕСТО
    # planner_provider (Mercury) — per-tenant эксперимент по образцу react_loop_enabled_tenants.
    # Пусто (дефолт) → никому → все на planner_provider (ноль изменений). Планировщик НЕ затронут.
    react_osa_tenants_raw: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SREDA_REACT_OSA_TENANTS",
            "sreda_react_osa_tenants",
        ),
    )
    # #184: Оса (gpt-oss-120b @ Groq) как FALLBACK для Фредди (Mercury) в ReAct — при сбое primary
    # (timeout/5xx) ход уходит на Осу. True → fallback включён для всех react-тенантов; False
    # (дефолт) → без fallback (как сейчас). Независим от react_osa_tenants (там Оса как PRIMARY).
    react_osa_fallback: bool = Field(
        default=False, validation_alias="SREDA_REACT_OSA_FALLBACK"
    )
    # #192: durable структурный трейс хода ReAct (react_turn_trace). True → пишем трейс (start/pause/
    # finish) И НЕ пишем временный react_debug_turns (#185, одна система). False (дефолт) → старый
    # путь #185 (откат). Раскат как #185: проверка → ВКЛ глобально.
    react_trace_enabled: bool = Field(
        default=False, validation_alias="SREDA_REACT_TRACE_ENABLED"
    )
    # #193: durable персистентность диалога ReAct (свой EncryptedSqlCheckpointSaver вместо
    # InMemorySaver). OFF (дефолт) → прежнее поведение: InMemory + ключ {base}#{gen}. ON →
    # стабильный durable-ключ f"react-v1:{base}" (версия топологии в ПРЕФИКСЕ; checkpoint_ns
    # зарезервирован LangGraph под подграфы), диалог переживает рестарт. ReAct-only;
    # НЕ конфликтует с легасовыми SREDA_LANGGRAPH_* (те гейтят отдельный legacy-граф).
    react_persist_enabled: bool = Field(
        default=False, validation_alias="SREDA_REACT_PERSIST_ENABLED"
    )
    # #194: компакция истории ReAct как prompt-view (структурный конвейер БЕЗ LLM). OFF (дефолт) →
    # модель получает всю историю (как сейчас). ON → урезанный валидный вид в пределах char-бюджета
    # (снижает prompt_too_long; полное устранение — L4). ReAct-only; канон #193 не мутируется.
    react_compact_enabled: bool = Field(
        default=False, validation_alias="SREDA_REACT_COMPACT_ENABLED"
    )
    # #194: переопределение char-бюджета компакции без передеплоя (калибровка/наблюдение). 0 (дефолт) →
    # кодовая константа TOTAL_BUDGET_CHARS. Поставить низкое значение → компакция сработает на коротком
    # диалоге (увидеть в логах react_compaction); вернуть в норму/0 после замера.
    react_compact_budget_chars: int = Field(
        default=0, validation_alias="SREDA_REACT_COMPACT_BUDGET_CHARS"
    )
    # #247: кеш-дисциплина. OFF (дефолт) → section-hint #215 + guard-нудж дописываются в СИСТЕМНЫЙ промпт
    # (нестабильный префикс каждый ход → ломается кеш большого префикса Mercury). ON → системный промпт
    # СТАБИЛЕН, директивы уходят в ХВОСТ (отдельным сообщением после истории) → кеш-префикс не рушится,
    # инструкция-следование даже лучше (свежесть). Дефолт OFF = byte-identical.
    react_tail_directives_enabled: bool = Field(
        default=False, validation_alias="SREDA_REACT_TAIL_DIRECTIVES"
    )
    # #197: preflight intent-router. OFF (дефолт) → прежнее: один Фредди, полный набор инструментов
    # (byte-identical через effective_intent=None — даже при чекпойнте с intent=chat). ON → классификатор
    # намерения (task/chat/fact): task→Фредди+полный набор; chat/fact→deepseek+web-only+поиск≤1
    # (анти-флейл, инцидент 2026-06-23). ReAct-only; legacy plan-execute не тронут. Раскат спящим.
    react_preflight_enabled: bool = Field(
        default=False, validation_alias="SREDA_REACT_PREFLIGHT_ENABLED"
    )
    # #197: провайдер рассуждающей модели для chat/fact-пути (eval #173 → deepseek-v4-flash). Строится
    # через get_chat_llm(provider=...). Недоступен/мисконфиг → fail-open в task (Фредди), scope web-only.
    react_preflight_chat_provider: str = Field(
        default="openrouter-deepseek", validation_alias="SREDA_REACT_PREFLIGHT_CHAT_PROVIDER"
    )
    # #221 Ф3: режим доменного скоупинга инструментов (ОТДЕЛЬНЫЙ от preflight #197, R4 Codex medium — не
    # сворачивать в один флаг, иначе миграция сломает #197). Три состояния:
    #   "disabled" (дефолт) — точный legacy: старый _route_families, БЕЗ нового роутера/логов → byte-identical;
    #   "shadow" — legacy-исполнение + sidecar-лог решения нового роутера (только свежий ход, try/except,
    #             НЕ мутирует state) → сравнение расхождений перед включением;
    #   "execute" — новый ontology-роутер драйвит active_families + _apply_domain_policy на bind-сайтах.
    # Активен ТОЛЬКО при react_preflight_enabled (домены — надстройка на task-пути). Невалидное значение →
    # "disabled" (fail-safe). Старый bool preflight НЕ затронут (миграция: старый прод-env сохраняет #197,
    # домен-режим по умолчанию disabled — R4 golden env-parsing).
    react_domain_scope_mode: str = Field(
        default="disabled", validation_alias="SREDA_REACT_DOMAIN_SCOPE_MODE"
    )

    @property
    def react_domain_scope(self) -> str:
        """Нормализованный режим доменного скоупинга ∈ {disabled, shadow, execute}; иначе → disabled (fail-safe)."""
        v = (self.react_domain_scope_mode or "").strip().lower()
        return v if v in ("disabled", "shadow", "execute") else "disabled"

    # #298: дата+время ЭФЕМЕРНЫМ хвостом контекста. OFF (дефолт) → легаси: дата в системном промпте
    # (рвёт кеш раз в сутки), времени нет нигде (модель выдумывает «сейчас 14:00» ночью - прод-кейс
    # 2026-07-03). ON → системный промпт ПОЛНОСТЬЮ стабилен (даты нет), «Сейчас YYYY-MM-DD (день)
    # HH:MM (МСК)» приклеивается к последнему user-сообщению на invoke (prompt-view, НЕ персистится,
    # префикс-кеш цел). Оба пути: task-граф + chat/fact. Откат = выключить флаг.
    react_time_in_tail_enabled: bool = Field(
        default=False, validation_alias="SREDA_REACT_TIME_IN_TAIL"
    )
    # #213 Срез A: унификация read-инструментов чек-листов. OFF (дефолт) → точный legacy: LLM видит
    # пару list_checklists/show_checklist, get_checklist не экспонирован, форма ответов байт-в-байт.
    # ON → LLM видит ЕДИНЫЙ get_checklist(mode: items|overview, name) с result-envelope; старые имена
    # скрыты из экспозиции, LLM-origin вызовы старых имён канонизируются в get_checklist (durable-история
    # может их праймить, #193/#221). Internal-пути (handlers/replay/eval) от флага НЕ зависят.
    # Откат = выключить флаг (без миграций/реверта реестра). Канарейка — срез C (plans/213-cycle-final.md).
    checklist_unified_enabled: bool = Field(
        default=False, validation_alias="SREDA_CHECKLIST_UNIFIED"
    )
    # #213 Срез B: предслойный сигнал query_kind + soft cross-check + write-enforcement.
    # OFF (дефолт) → fail-open: голая схема среза A (или полный легаси при unified=OFF).
    # ON (работает ТОЛЬКО при unified=ON + preflight): предслой детерминированными правилами
    # эмитит checklist_query_kind (items|overview|search|mixed) + resolved span; исполнитель
    # сверяет mode вызова с интентом (mismatch → структурный отказ), редиректит ТОЛЬКО
    # отсутствующее имя (exact|unique_fuzzy по span юзера), гейтит write ordinal-привязкой
    # source_result_id. Матрица: UNIFIED=OFF/QUERYKIND=* → легаси; ON/OFF → схема без
    # cross-check; ON/ON → полный контур. Канарейка — срез C (plans/213-cycle-final.md).
    checklist_querykind_enabled: bool = Field(
        default=False, validation_alias="SREDA_CHECKLIST_QUERYKIND"
    )
    # #221 Ф4 (канареечная раскатка execute): даже при глобальном mode=execute РЕАЛЬНО драйвить роутер
    # только для тенантов из этого списка; остальные при execute ведут себя как shadow (лог-only). Так
    # execute катится постепенно (сперва тенант Бориса → проверка «дела» → ``*`` на всех). Пусто (дефолт)
    # → НИКОМУ execute (mode=execute без списка = глобальный shadow). ``*`` → ВСЕ (полная раскатка).
    react_domain_scope_execute_tenants_raw: str | None = Field(
        default=None, validation_alias="SREDA_REACT_DOMAIN_SCOPE_EXECUTE_TENANTS"
    )
    # #285 Фаза 0: единый путь ReAct (TurnPolicy). ОТДЕЛЬНЫЙ флаг — react_preflight_enabled НЕ
    # переиспользуем: его семантика занята (ON = сплит #197, OFF = kill-switch → до-#197 full-bind
    # byte-identical), свободного значения под «единый путь» нет (тот же урок R4, что у домен-режима
    # выше: не сворачивать в один флаг). OFF (дефолт) = ноль изменений, сплит #197 как есть;
    # ON → TurnPolicy-путь (Фаза A shadow → B канарейка; канареечный компаньон-список — при заходе
    # в Фазу A/B). react_preflight_enabled остаётся независимым kill-switch'ем сплита до Фазы E.
    react_unified_path_enabled: bool = Field(
        default=False, validation_alias="SREDA_REACT_UNIFIED_PATH_ENABLED"
    )
    # #285 Фаза A: канареечный companion-список единого пути (паттерн Ф4 #221). Семантика связки:
    # флаг OFF → полиси-кода на пути НЕТ вовсе (zero-overhead откат Фазы A);
    # флаг ON + список пуст (дефолт) → SHADOW для всех (полиси считается/логируется, НЕ управляет);
    # флаг ON + тенант в списке → execute (Фаза B; до неё ветки execute в коде не существует);
    # ``*`` → все (Фаза F). Дизайн-решение: plans/285-phaseA-design.md, Решение 1.
    react_unified_tenants_raw: str | None = Field(
        default=None, validation_alias="SREDA_REACT_UNIFIED_TENANTS"
    )
    # #149 M5: tenants whose substituted reply text may be previewed in admin
    # alerts. Dedicated privacy allowlist — NOT planner_enabled_tenants (that's
    # rollout, not "internal/PD-safe"; planner-enabling an external tenant must
    # not start leaking their reply text). Empty (default) → never preview.
    admin_alert_preview_tenants_raw: str | None = Field(
        default=None,
        validation_alias="SREDA_ADMIN_ALERT_PREVIEW_TENANTS",
    )
    # vex#170: ВРЕМЕННЫЙ debug-allowlist тенантов, чьи react-ходы (вопрос+ответ бота+инструменты)
    # сохраняются в react_debug_turns, чтобы видеть всю переписку пока тестируем механизм. Текст
    # шифруется. Дефолт пуст → НИКОМУ (защита: только явно перечисленные дебаг/тест-тенанты).
    # Удалить вместе с фичей после теста. НЕ глобальный флаг (privacy defense-in-depth, Codex R1).
    react_debug_tenants_raw: str | None = Field(
        default=None,
        validation_alias="SREDA_REACT_DEBUG_TENANTS",
    )
    # #185 (pre-launch QA, ВРЕМЕННО): ГЛОБАЛЬНЫЙ захват переписки. True → _persist_debug_turn пишет
    # ходы ВСЕХ тенантов (а не только allowlist выше) — производственная отладка/отлов багов перед
    # запуском, снять после. Текст шифруется (EncryptedString). Оферта/согласие ПД на запуске ДОЛЖНЫ
    # покрывать захват переписки. Дефолт False (без флага — прежнее поведение, только allowlist).
    react_debug_all: bool = Field(
        default=False, validation_alias="SREDA_REACT_DEBUG_ALL"
    )
    # #165 Срез B: тенанты с ОБРЕЗКОЙ набора инструментов (ленивая загрузка семей: ядро +
    # предзагруженные словарём top-2 + добор need_family/guard). Дефолт пуст → НИКОМУ: все
    # на full-bind (весь набор привязан, как до #165 — ноль изменений). Канарейка/kill-switch:
    # добавить/убрать тенанта здесь. НЕ глобальный (per-tenant rollout, p-004 tenant↔user 1:1).
    react_prune_tenants_raw: str | None = Field(
        default=None,
        validation_alias="SREDA_REACT_PRUNE_TENANTS",
    )
    # #232 способ Б: тенанты с durable-выжимкой истории (генерация+потребление). Дефолт None → НИКОМУ.
    react_summary_tenants_raw: str | None = Field(
        default=None,
        validation_alias="SREDA_REACT_SUMMARY_TENANTS",
    )

    # Plan-Execute LLM provider split (Hakuoro357/vex-assistant#77 item #5).
    # Planner needs heavyweight reasoning + structured-output compliance —
    # composer just renders free text from execution_log. Separating them
    # lets us pay light-model price on every reply while keeping plan
    # quality on the planner side.
    #
    # Provider keys follow ``services/llm.py`` registry. After the
    # 2026-05-29 rename, provider key == model name (``mimo-v2.5-pro`` →
    # model mimo-v2.5-pro; ``mimo-v2.5`` → plain mimo-v2.5).
    planner_provider: str = Field(
        # 2026-05-29 rename (Boris): the planner stays on the PRO model.
        # The provider key for pro is now ``mimo-v2.5-pro`` (was the
        # confusingly-named ``mimo-v2.5`` which silently mapped to -pro).
        default="mimo-v2.5-pro",
        validation_alias=AliasChoices(
            "SREDA_PLANNER_PROVIDER",
            "sreda_planner_provider",
        ),
        description=(
            "LLM provider key for the planner node (heavyweight model — needs "
            "multi-step reasoning + json-schema-strict output). Cheaper "
            "variants may break Plan schema compliance on edge cases; "
            "benchmark before downgrading. Must be a provider key from "
            "services/llm.py registry. Default 'mimo-v2.5-pro' → model "
            "mimo-v2.5-pro. (Plain 'mimo-v2.5' is the lighter model, used by "
            "the composer.)"
        ),
    )
    planner_persist_executions: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "SREDA_PLANNER_PERSIST_EXECUTIONS",
            "sreda_planner_persist_executions",
        ),
        description=(
            "#155 (SIA): when True, the live planner path writes ONE "
            "planner_executions row at end of turn (plan + per-step execution "
            "log) for the offline feedback loop. Default off; enabled per "
            "rollout on the internal planner tenants."
        ),
    )
    planner_verbose_log: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "SREDA_PLANNER_VERBOSE_LOG",
            "sreda_planner_verbose_log",
        ),
        description=(
            "When True, the planner orchestrator logs per-attempt provider+model, "
            "the resolved plan (tool names + args + clarity + compose target), and "
            "validator/parse violations to the app logger (job-runner.log) — "
            "observability for the plan path. Plan args may contain user content "
            "(same as agent_runs.input_json), logged as-is; secret-redaction (#95) "
            "covers Telegram bot-tokens only, not arbitrary user secrets. "
            "Default True (planner runs only for the small planner-enabled tenant "
            "set). Set False to silence."
        ),
    )
    planner_timeout_sec: float = Field(
        default=60.0,
        ge=1.0,
        le=600.0,
        validation_alias=AliasChoices(
            "SREDA_PLANNER_TIMEOUT_SEC",
            "sreda_planner_timeout_sec",
        ),
        description=(
            "Wall-clock cap for a single planner LLM call, in seconds. "
            "Default 60s matches the legacy ``invoke_with_per_call_timeout`` "
            "default. Sub-A12 Phase B.2 (planner LLM wrapper)."
        ),
    )
    react_llm_timeout_sec: float = Field(
        default=60.0,
        ge=1.0,
        le=600.0,
        validation_alias=AliasChoices(
            "SREDA_REACT_LLM_TIMEOUT_SEC",
            "sreda_react_llm_timeout_sec",
        ),
        description=(
            "Wall-clock cap for a single ReAct chat-node LLM call, in seconds. "
            "Default 60s matches the legacy ``invoke_with_per_call_timeout`` "
            "default and ``planner_timeout_sec``. #159 п.1: bounds a hung/slow "
            "primary (Mercury/deepseek) so the turn falls over to the #184 "
            "fallback (Osa/Freddie) instead of hanging. Applies to both the "
            "task and chat/fact branches, primary and fallback invokes."
        ),
    )
    react_chat_llm_timeout_sec: float = Field(
        default=15.0,
        ge=1.0,
        le=600.0,
        validation_alias=AliasChoices(
            "SREDA_REACT_CHAT_LLM_TIMEOUT_SEC",
            "sreda_react_chat_llm_timeout_sec",
        ),
        description=(
            "#256: separate, SHORTER wall-clock cap for the chat/fact branch "
            "LLM calls (primary + fallback), in seconds. Default 15s — the "
            "chat/fact reasoning model is normally 1-3s, so a hung primary "
            "fails over to the #184 Freddie web-only fallback fast instead of "
            "the user waiting the full task cap (``react_llm_timeout_sec``, "
            "60s — kept long for Mercury multi-hop tool turns). Misconfig → "
            "code falls back to a safe default."
        ),
    )
    # --- #244: fetch_url SSRF egress + per-day квота -------------------------
    # Маршрут fetch_url ТОЛЬКО через выделенный фильтр-egress туннель (C/nft на
    # выходной ноде). Пусто → fetch FAIL-CLOSED (не direct/не env). Env —
    # короткое ops-имя SREDA_FETCH_URL_PROXY (НЕ ..._URL), задаётся на шаге 5
    # gate-раскатки ПОСЛЕ того как nft+туннель PORT2 подняты и probes зелёные.
    react_fetch_url_proxy_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SREDA_FETCH_URL_PROXY",
            "sreda_fetch_url_proxy",
        ),
        description=(
            "#244: socks5://127.0.0.1:<PORT2> — ЕДИНСТВЕННЫЙ egress для fetch_url "
            "(фильтр-туннель на выходной ноде). Пусто → fetch fail-closed."
        ),
    )
    react_fetch_url_per_day_cap: int = Field(
        default=50,
        ge=1,
        le=100000,
        validation_alias=AliasChoices(
            "SREDA_REACT_FETCH_URL_PER_DAY_CAP",
            "sreda_react_fetch_url_per_day_cap",
        ),
        description=(
            "#244: дневной лимит fetch_url для FREE-тира (анти-злоупотребление; "
            "paid/grandfathered — без лимита). NO-REFUND. Дефолт 50 — env-tunable."
        ),
    )
    react_fetch_url_per_turn_cap: int = Field(
        default=5,
        ge=1,
        le=100,
        validation_alias=AliasChoices(
            "SREDA_REACT_FETCH_URL_PER_TURN_CAP",
            "sreda_react_fetch_url_per_turn_cap",
        ),
        description=(
            "#244: storm-cap fetch_url в ОДНОМ ходе (#211) — отсекает шторм "
            "параллельных fetch (paid/grandfathered иначе без дневного предела). Дефолт 5."
        ),
    )
    planner_prompt_version: int = Field(
        default=1,
        ge=1,
        validation_alias=AliasChoices(
            "SREDA_PLANNER_PROMPT_VERSION",
            "sreda_planner_prompt_version",
        ),
        description=(
            "Active planner system-prompt revision. Stored in "
            "``planner_executions.planner_prompt_version`` so audit/replay "
            "can pin which prompt produced a given plan. Bump when "
            "system prompt changes meaningfully (not on cosmetic edits)."
        ),
    )
    planner_invalid_retry_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "SREDA_PLANNER_INVALID_RETRY_ENABLED",
            "sreda_planner_invalid_retry_enabled",
        ),
        description=(
            "Whether the orchestrator should retry once with explicit "
            "error feedback when parse/validate/compile fails. Default "
            "True (production). Set False in tests/eval that assert "
            "single-attempt behavior. Sub-A12 Phase B.4."
        ),
    )
    planner_alerts_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "SREDA_PLANNER_ALERTS_ENABLED",
            "sreda_planner_alerts_enabled",
        ),
        description=(
            "Whether orchestrator sends P1 admin alerts on final failure "
            "(invalid plan after retry, provider unavailable, timeout, "
            "persistence outage). Default False — offline/library mode "
            "and eval scripts don't spam admin channels. Enable on "
            "production rollout via env. Sub-A12 R3 #9."
        ),
    )
    composer_llm_enabled_keys_raw: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SREDA_COMPOSER_LLM_ENABLED_KEYS",
            "sreda_composer_llm_enabled_keys",
        ),
        description=(
            "Per-key allow-list for the composer LLM-path (kind='llm'), "
            "comma-separated llm_prompt_keys. Default EMPTY → the "
            "planner-flow ships TEMPLATE-ONLY (structurally safe, no "
            "free-text generation). The orchestrator intersects this with "
            "the LLM prompt registry and the caller's proposed keys, then "
            "uses the result for BOTH the planner prompt AND validate_plan "
            "— so a caller cannot bypass the gate (Codex D.2-enable R2 "
            "HIGH). Roll out ONE curated key at a time (e.g. "
            "'recipe_narrative'); each key needs its provenance contract "
            "(Task #34) before it goes live. Empty → planner never sees a "
            "key AND validate_plan rejects any kind='llm'. Decoupled from "
            "planner_mode so enabling planner-flow does NOT implicitly "
            "enable free-text compose."
        ),
    )
    composer_provider: str = Field(
        default="mimo-v2.5-pro",
        validation_alias=AliasChoices(
            "SREDA_COMPOSER_PROVIDER",
            "sreda_composer_provider",
        ),
        description=(
            "LLM provider for the composer LLM-path (ComposerCall.kind='llm'). "
            "Composer writes free-text replies from a finished execution_log "
            "— no tool selection, no multi-step reasoning. Default "
            "'mimo-v2.5-pro' (Boris decision 2026-06-06: run the composer on the "
            "pro tier for higher reply quality; previously the lighter plain "
            "'mimo-v2.5' was used for ~2x speed). Provider key == model name "
            "after the 2026-05-29 rename. mimo-flash is NOT on our subscription "
            "tier. Template-path composer calls don't touch any LLM at all. "
            "NOTE: pro is ~2x slower than plain mimo-v2.5 — watch "
            "composer_timeout_sec (still 30s)."
        ),
    )
    composer_timeout_sec: float = Field(
        default=30.0,
        ge=1.0,
        le=600.0,
        validation_alias=AliasChoices(
            "SREDA_COMPOSER_TIMEOUT_SEC",
            "sreda_composer_timeout_sec",
        ),
        description=(
            "Wall-clock cap for a single composer LLM call (kind='llm'), "
            "in seconds. Default 30s — half the planner cap because the "
            "composer does no tool selection / multi-step reasoning, just "
            "free-text narration over a finished execution_log on a lighter "
            "model (mimo-v2.5, the plain non-pro tier). Template-path "
            "composer calls don't "
            "touch any LLM and ignore this. Sub-A12 Phase D.2."
        ),
    )

    # LangGraph PostgresSaver wiring (Sub-A6 — Hakuoro357/vex-assistant#74).
    # Default behaviour: postgres URL → PostgresSaver, anything else
    # → InMemorySaver. Override to "memory" to force in-memory even
    # with a postgres URL (test / dev path).
    langgraph_checkpointer_mode: str = Field(
        default="auto",
        validation_alias=AliasChoices(
            "SREDA_LANGGRAPH_CHECKPOINTER",
            "sreda_langgraph_checkpointer",
        ),
        description="'auto' = memory unless persistence_opted_in; 'memory' = always memory.",
    )
    langgraph_persistence_opted_in: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "SREDA_LANGGRAPH_PERSISTENCE_OPTED_IN",
            "sreda_langgraph_persistence_opted_in",
        ),
        description=(
            "Explicit consent gate for PostgresSaver. LangGraph state "
            "contains decrypted PII (profile, memories, replies). Until "
            "we add an encrypted serializer (issue TBD), default is "
            "False — checkpointer stays in-memory even with postgres "
            "URL. Codex R1 CRITICAL 2026-05-26."
        ),
    )
    langgraph_pool_max_size: int = Field(
        default=10,
        ge=1,
        le=100,
        validation_alias=AliasChoices(
            "SREDA_LANGGRAPH_POOL_MAX_SIZE",
            "sreda_langgraph_pool_max_size",
        ),
        description="psycopg ConnectionPool max_size for PostgresSaver. Default 10 fits single-VDS; bump for worker scale-out.",
    )

    # Sub-A9 / Group 6.6 — conversation_turn hard close + soft hint thresholds.
    turn_hard_close_days: int = Field(
        default=30,
        ge=1,
        le=365,
        validation_alias=AliasChoices(
            "SREDA_TURN_HARD_CLOSE_DAYS",
            "sreda_turn_hard_close_days",
        ),
        description=(
            "Group 6.6 Codex MAJOR #7 — deterministic close for active "
            "turns older than N days. Without this a runaway open turn "
            "could carry month-old context into a fresh conversation."
        ),
    )
    turn_hint_threshold_hours: int = Field(
        default=6,
        ge=1,
        le=168,
        validation_alias=AliasChoices(
            "SREDA_TURN_HINT_THRESHOLD_HOURS",
            "sreda_turn_hint_threshold_hours",
        ),
        description=(
            "Group 5.1 — soft warning threshold. If the active turn's "
            "last message is older than this, the planner-prompt builder "
            "adds a hint suggesting the planner consider is_new_turn=true."
        ),
    )

    # Speech recognition provider:
    #   ``yandex``        — Yandex SpeechKit only (current default).
    #   ``groq``          — Groq Whisper only (~0.3-0.5s vs Yandex ~1-2s).
    #   ``groq+yandex``   — Groq primary, Yandex fallback on any error.
    #   None              — STT disabled (voice messages ignored).
    speech_provider: str | None = Field(default=None, validation_alias="SREDA_SPEECH_PROVIDER")
    yandex_speechkit_api_key: str | None = Field(
        default=None, validation_alias="SREDA_YANDEX_SPEECHKIT_API_KEY"
    )
    # Yandex Cloud Billing — показ остатка ₽ в /admin/llm (#229). Авторизованный
    # ключ сервисного аккаунта (PEM с заголовком «… SA Key ID <id>» ИЛИ JSON),
    # которым подписывается JWT → IAM-токен → Billing API. SA-id нужен ТОЛЬКО
    # для PEM-формата (в JSON он внутри). account_id опционален — если не задан,
    # берётся первый аккаунт из List.
    yandex_billing_sa_key_file: str | None = Field(
        default=None, validation_alias="SREDA_YANDEX_BILLING_SA_KEY_FILE"
    )
    yandex_billing_sa_id: str | None = Field(
        default=None, validation_alias="SREDA_YANDEX_BILLING_SA_ID"
    )
    yandex_billing_account_id: str | None = Field(
        default=None, validation_alias="SREDA_YANDEX_BILLING_ACCOUNT_ID"
    )
    # Groq Whisper — OpenAI-compatible /audio/transcriptions endpoint on
    # LPU hardware. Same resolve-precedence as MiMo: explicit key first,
    # then file path, then disabled.
    groq_api_key: str | None = Field(default=None, validation_alias="SREDA_GROQ_API_KEY")
    groq_api_key_file: str | None = Field(
        default=None, validation_alias="SREDA_GROQ_API_KEY_FILE"
    )

    # Tavily AI — API search tool для LLM-агентов (заменил DuckDuckGo
    # 2026-04-29 после Bing-403 c RU egress). Free tier 1000 query/мес.
    # Используется в `services/web_search_tool.build_web_search_tool`,
    # квота отслеживается в `WebSearchUsageCounter` (per-user 30/мес,
    # global 950/мес). При превышении — fallback на DDG `backend="api"`.
    tavily_api_key: str | None = Field(
        default=None, validation_alias="TAVILY_API_KEY"
    )

    # #211: жёсткий лимит web_search на ОДИН ход ReAct (для ВСЕХ тиров) —
    # отсечка шторма. Один спутанный ход загонял 35 веб-поисков и выжирал
    # Tavily-квоту; per-user стоп есть не у всех (grandfathered). 0 — без
    # per-ход лимита (только дневная/месячная квота). Тюнится без редеплоя.
    react_web_search_per_turn_cap: int = Field(
        default=4, validation_alias="SREDA_REACT_WEB_SEARCH_PER_TURN_CAP"
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

    # Trace log: per-turn multi-line timing block for user conversations
    # (entry → voice → LLM iters → outbox → delivery). One block per
    # completed turn, written by ``services.trace.emit_block``. Opt-in
    # via ``SREDA_TRACE_LOG_PATH``; если None — sreda.trace пишет только
    # в общий access stream без отдельного файла.
    trace_log_path: str | None = None

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

    @model_validator(mode="after")
    def _check_llm_trace_compliance_pair(self) -> "Settings":
        """Issue #68 (R7 M-R6-3): compliance-strict mode (require_persist=true)
        требует чтобы logging был включён. Misconfig = silent fail-open: caller
        получит WRITTEN (no-op success), turn продолжится без trace, нарушит
        compliance гарантию.

        Fail loud at Settings construction time. FastAPI startup упадёт явно
        вместо тихого деградирования в prod.
        """
        if self.llm_trace_require_persist and not self.llm_trace_logging_enabled:
            raise ValueError(
                "Misconfiguration: SREDA_LLM_TRACE_REQUIRE_PERSIST=true requires "
                "SREDA_LLM_TRACE_LOGGING_ENABLED=true. Either enable logging or "
                "remove require_persist flag."
            )
        return self

    @field_validator("max_api_base_url")
    @classmethod
    def _validate_max_api_base_url(cls, value: str) -> str:
        # #214: на этот адрес уходит токен MAX-бота (Authorization header).
        # Плохое значение из env = утечка токена или открытый текст, поэтому
        # fail-fast на загрузке конфига строгими правилами:
        #   * scheme строго https (иначе токен ушёл бы в открытом виде);
        #   * host обязателен и только в зоне max.ru (не чужой хост);
        #   * без userinfo (user:pass@) — анти-инъекция.
        from urllib.parse import urlsplit

        # Точный allowlist хостов API MAX (Codex R2): новый + адрес отката.
        # Токен-несущий endpoint — пускаем ТОЛЬКО известные хосты, не любой
        # *.max.ru (иначе плохой env мог бы отправить токен на другой сервис MAX).
        _ALLOWED_HOSTS = {"platform-api2.max.ru", "platform-api.max.ru"}
        candidate = (value or "").strip()
        parts = urlsplit(candidate)
        if parts.scheme != "https":
            raise ValueError(
                "max_api_base_url должен быть https:// — иначе токен MAX "
                "уйдёт в открытом виде"
            )
        if parts.username or parts.password:
            raise ValueError("max_api_base_url не должен содержать userinfo")
        host = (parts.hostname or "").lower()
        if host not in _ALLOWED_HOSTS:
            raise ValueError(
                f"max_api_base_url host должен быть одним из {sorted(_ALLOWED_HOSTS)}, "
                f"получено: {host!r}"
            )
        if parts.port not in (None, 443):
            raise ValueError(
                f"max_api_base_url: нестандартный порт {parts.port!r} не допускается"
            )
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("max_api_base_url не должен содержать path/query/fragment")
        return candidate

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
    def message_queue_enabled_tenants(self) -> frozenset[str]:
        """Tenants whose inbound messages go through the new FIFO queue.

        Empty set → universal legacy inline path (current behaviour).
        Workers and the refactored poller call ``is_queue_enabled_for(...)``
        which checks membership in this set.
        """
        raw = self.message_queue_enabled_tenants_raw
        if not raw:
            return frozenset()
        return frozenset(item.strip() for item in raw.split(",") if item.strip())

    @property
    def composer_llm_enabled_keys(self) -> frozenset[str]:
        """Allow-listed composer ``llm_prompt_key`` values (kind='llm').

        Empty → composer LLM-path OFF (template-only). The orchestrator
        intersects this with the LLM prompt registry + the caller's
        proposed keys and uses the result for prompt + validation, so
        the gate is non-bypassable. Sub-A12 D.2-enable R2."""
        raw = self.composer_llm_enabled_keys_raw
        if not raw:
            return frozenset()
        return frozenset(item.strip() for item in raw.split(",") if item.strip())

    @property
    def planner_enabled_tenants(self) -> frozenset[str]:
        """Tenants whose worker-side processing uses the planner-flow.

        Only effective when the same tenant is also in
        ``message_queue_enabled_tenants`` — the planner runs inside the
        worker, so a tenant on the legacy inline path cannot reach it.
        Empty set → everyone stays on legacy ReAct (current behaviour).
        """
        raw = self.planner_enabled_tenants_raw
        if not raw:
            return frozenset()
        return frozenset(item.strip() for item in raw.split(",") if item.strip())

    @property
    def react_loop_enabled_tenants(self) -> frozenset[str]:
        """#66: tenants on the new ReAct+interrupt conversational loop. Empty
        (default) → nobody; ``*`` → ВСЕ тенанты, включая будущие (#159-rollout)."""
        return _parse_tenant_gate(self.react_loop_enabled_tenants_raw)

    @property
    def react_osa_tenants(self) -> frozenset[str]:
        """#184: тенанты, чей ReAct идёт на «Осе» (gpt-oss-120b @ Groq) вместо planner_provider.
        Пусто (дефолт) → никому → все на planner_provider (Mercury)."""
        raw = self.react_osa_tenants_raw
        if not raw:
            return frozenset()
        return frozenset(item.strip() for item in raw.split(",") if item.strip())

    @property
    def react_debug_tenants(self) -> frozenset[str]:
        """vex#170 (ВРЕМЕННОЕ): тенанты, чьи react-ходы сохраняются в react_debug_turns для
        дебага. Пусто (дефолт) → НИКОМУ (не глобально — privacy allowlist)."""
        raw = self.react_debug_tenants_raw
        if not raw:
            return frozenset()
        return frozenset(item.strip() for item in raw.split(",") if item.strip())

    @property
    def react_prune_tenants(self) -> frozenset[str]:
        """#165 Срез B: тенанты с обрезкой набора инструментов (ленивая загрузка семей).
        Пусто (дефолт) → НИКОМУ → full-bind; ``*`` → ВСЕ тенанты (#159-rollout, экономия
        токенов; измеренно безопасно: −89% токенов, паритет выбора инструмента)."""
        return _parse_tenant_gate(self.react_prune_tenants_raw)

    @property
    def react_summary_tenants(self) -> frozenset[str]:
        """#232 способ Б: тенанты с durable-выжимкой истории (генерация + потребление). Пусто (дефолт) →
        НИКОМУ (фича OFF, потребление байт-идентично #194); ``*`` → ВСЕ. Канарейка/kill-switch."""
        return _parse_tenant_gate(self.react_summary_tenants_raw)

    @property
    def react_domain_scope_execute_tenants(self) -> frozenset[str]:
        """#221 Ф4: тенанты, на которых доменный роутер РЕАЛЬНО драйвит (execute) при глобальном
        mode=execute. Пусто (дефолт) → НИКОМУ execute (mode=execute = глобальный shadow); ``*`` → ВСЕ
        (полная раскатка). Канареечный гейт: сперва тенант Бориса, после проверки «дела» → ``*``."""
        return _parse_tenant_gate(self.react_domain_scope_execute_tenants_raw)

    @property
    def react_unified_tenants(self) -> frozenset[str]:
        """#285 Фаза A/B: тенанты execute единого пути при включённом react_unified_path_enabled.
        Пусто (дефолт) → НИКОМУ execute (флаг ON = глобальный SHADOW); ``*`` → ВСЕ (Фаза F).
        В Фазе A ветки execute не существует — список задел на B."""
        return _parse_tenant_gate(self.react_unified_tenants_raw)

    @property
    def admin_alert_preview_tenants(self) -> frozenset[str]:
        """#149 M5: tenants whose substituted reply text may appear in admin
        alerts (privacy allowlist). Empty (default) → no preview for anyone."""
        raw = self.admin_alert_preview_tenants_raw
        if not raw:
            return frozenset()
        return frozenset(item.strip() for item in raw.split(",") if item.strip())

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

    def resolve_inception_api_key(self) -> str | None:
        """Resolve Inception (Mercury) API key, env→file→None — как mimo."""
        if self.inception_api_key:
            return self.inception_api_key.strip()
        if self.inception_api_key_file:
            from pathlib import Path

            path = Path(self.inception_api_key_file)
            if path.exists() and path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                return value or None
        return None

    def resolve_groq_api_key(self) -> str | None:
        """Resolve Groq API key with the same env→file→None precedence
        as ``resolve_mimo_api_key``. File path is relative to CWD."""
        if self.groq_api_key:
            return self.groq_api_key.strip()
        if self.groq_api_key_file:
            from pathlib import Path

            path = Path(self.groq_api_key_file)
            if path.exists() and path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                return value or None
        return None

    def resolve_openrouter_api_key(self) -> str | None:
        """Resolve OpenRouter key with the same env→file→None
        precedence. OpenRouter tokens live on disk in markdown so the
        file path is usually ``.secrets/openrouter-token.md``; the
        first non-empty stripped line of that file is returned."""
        if self.openrouter_api_key:
            return self.openrouter_api_key.strip()
        if self.openrouter_api_key_file:
            from pathlib import Path

            path = Path(self.openrouter_api_key_file)
            if path.exists() and path.is_file():
                for line in path.read_text(encoding="utf-8").splitlines():
                    value = line.strip()
                    if value:
                        return value
        return None

    def __repr_args__(self):
        """Pydantic v2 BaseModel composes ``__repr__`` from this method.
        Mask secret-typed fields so accidental ``logger.info(settings)``,
        traceback locals, or ``print(settings)`` don't leak credentials.

        Non-secret fields are returned as-is. Secret fields show ``***``
        when value is set, ``None`` when unset (so misconfiguration is
        still visible without exposing the actual value).
        """
        for name, value in super().__repr_args__():
            if name in _SECRET_FIELD_NAMES and value is not None:
                yield name, "***"
            else:
                yield name, value


@lru_cache
def get_settings() -> Settings:
    return Settings()
