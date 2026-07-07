import logging
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from sreda.config.settings import get_settings

logger = logging.getLogger(__name__)

# ── #138 ContextVar-шов изоляции семей (RLS) ──────────────────────────────
# tenant_ctx выставляется tenant_session'ом; begin-событие app-движка эмитит
# `set_config('sreda.tenant_id', tid, true)` (txn-scoped). tenant_ctx=None
# (легаси-пути / privileged) → no-op → RLS-политика вернёт 0 строк (fail-closed).
tenant_ctx: ContextVar[str | None] = ContextVar("sreda_tenant_id", default=None)
access_ctx: ContextVar[str] = ContextVar("sreda_access_mode", default="tenant")

# Явный allow-list reason'ов privileged_session (fail-closed + аудит). Маленький,
# аудитопригодный. identity-reason'ы идут на identity-движок, прочие — maintenance.
_IDENTITY_REASONS: frozenset[str] = frozenset({"inbound-provision", "identity-resolve"})
PRIVILEGED_REASONS: frozenset[str] = _IDENTITY_REASONS | frozenset({
    "gc",             # GC чекпоинтов/summary
    "retention",      # ретеншн #127
    "admin",          # админка/дашборд
    "monitor",        # мониторы/probe
    "broadcast",      # рассылки
    "migration-data",  # data-миграции
    "queue-dispatch",  # консьюмер очереди message_jobs (claim/mark/heartbeat — кросс-тенант)
})


def _build_engine(database_url: str):
    connect_args: dict[str, object] = {}
    engine_kwargs: dict[str, object] = {"future": True, "connect_args": connect_args}
    if database_url.startswith("sqlite"):
        connect_args["timeout"] = 30
        # SQLite keeps its own pool class (QueuePool sizing args don't apply).
    elif database_url.startswith(("postgresql", "postgres")):
        # PostgreSQL: explicit, env-tunable pool (see prior note on Mini App bursts).
        settings = get_settings()
        engine_kwargs["pool_size"] = settings.db_pool_size
        engine_kwargs["max_overflow"] = settings.db_max_overflow
        engine_kwargs["pool_timeout"] = settings.db_pool_timeout
        engine_kwargs["pool_pre_ping"] = settings.db_pool_pre_ping

    engine = create_engine(database_url, **engine_kwargs)
    if database_url.startswith("sqlite"):
        _configure_sqlite_engine(engine)
    return engine


def _emit_tenant_setconfig(conn) -> None:
    """#138 begin-событие app-движка: ставит `sreda.tenant_id` из ContextVar на КАЖДОЙ
    транзакции (txn-scoped, is_local=true — безопасно при переиспользовании pool-соединения
    тенантом B). tenant_ctx=None (легаси/privileged) → no-op. Не-Postgres (unit-SQLite) → no-op."""
    tid = tenant_ctx.get()
    if tid is None:
        return
    if conn.dialect.name != "postgresql":
        return
    conn.execute(text("SELECT set_config('sreda.tenant_id', :tid, true)"), {"tid": tid})


@lru_cache
def get_engine():
    """Канонический app-движок (пул). #138: на нём висит begin-событие шва."""
    engine = _build_engine(get_settings().database_url)
    event.listen(engine, "begin", _emit_tenant_setconfig)
    return engine


def get_app_engine():
    """#138 Р5: app DSN = database_url (рантайм под sreda_app). Переиспользуем пул get_engine."""
    return get_engine()


@lru_cache
def get_maintenance_engine():
    """#138: maintenance-роль. Пока maintenance_database_url не разведён — тот же движок (аддитивно, v1)."""
    settings = get_settings()
    url = getattr(settings, "maintenance_database_url", None) or settings.database_url
    if url == settings.database_url:
        return get_engine()
    return _build_engine(url)


@lru_cache
def get_identity_engine():
    """#138: узкая identity-роль (inbound-провижн). Пока identity_database_url не разведён — тот же движок."""
    settings = get_settings()
    url = getattr(settings, "identity_database_url", None) or settings.database_url
    if url == settings.database_url:
        return get_engine()
    return _build_engine(url)


@lru_cache
def _factory_for(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return _factory_for(get_engine())


def get_db_session() -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def tenant_session(tenant_id: str) -> Iterator[Session]:
    """#138: тенант-скоуп сессия на app-движке. Ставит tenant_ctx=tenant_id (→ begin-событие
    эмитит set_config) + access_ctx='tenant'. Сбрасывает ContextVar на выходе (в т.ч. на исключении)."""
    tok_t = tenant_ctx.set(tenant_id)
    tok_a = access_ctx.set("tenant")
    session = _factory_for(get_app_engine())()
    try:
        yield session
    finally:
        session.close()
        access_ctx.reset(tok_a)
        tenant_ctx.reset(tok_t)


@contextmanager
def privileged_session(reason: str) -> Iterator[Session]:
    """#138: привилегированная сессия (identity/maintenance-роль) — ТОЛЬКО по reason из реестра
    (fail-closed) + аудит-лог. access_ctx='privileged'. tenant_ctx НЕ ставит (RLS пускает по current_user)."""
    if reason not in PRIVILEGED_REASONS:
        raise PermissionError(f"privileged_session: reason not in registry: {reason!r}")
    logger.info("privileged_session opened reason=%s", reason)  # аудит (без ПД)
    engine = get_identity_engine() if reason in _IDENTITY_REASONS else get_maintenance_engine()
    # ГАСИМ tenant_ctx (Ф1-R1 MAJOR): если privileged открыт ВНУТРИ tenant_session(A), а движки
    # ещё фолбэкают на общий get_engine (до Ф5), begin-событие иначе выставило бы чужой
    # sreda.tenant_id=A в привилегированном блоке. privileged ходит по РОЛИ (current_user), не по GUC.
    tok_t = tenant_ctx.set(None)
    tok_a = access_ctx.set("privileged")
    session = _factory_for(engine)()
    try:
        yield session
    finally:
        session.close()
        access_ctx.reset(tok_a)
        tenant_ctx.reset(tok_t)


def assert_runtime_role(expected: str) -> None:
    """#138 Р4: рантайм-ассерт — процесс обязан быть под ожидаемой ролью БД, иначе fail-closed
    (кривой env под owner = RLS молча off). На не-Postgres (unit) — no-op."""
    eng = get_engine()
    if eng.dialect.name != "postgresql":
        return
    with eng.connect() as conn:
        role = conn.execute(text("SELECT current_user")).scalar()
    if role != expected:
        raise RuntimeError(f"runtime role assertion failed: current_user={role!r} expected={expected!r}")


def _configure_sqlite_engine(engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()
