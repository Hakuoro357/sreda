from functools import lru_cache
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from sreda.config.settings import get_settings


@lru_cache
def get_engine():
    settings = get_settings()
    database_url = settings.database_url
    connect_args: dict[str, object] = {}
    engine_kwargs: dict[str, object] = {"future": True, "connect_args": connect_args}
    if database_url.startswith("sqlite"):
        connect_args["timeout"] = 30
        # SQLite keeps its own pool class (QueuePool sizing args don't apply).
    elif database_url.startswith(("postgresql", "postgres")):
        # PostgreSQL: explicit, env-tunable pool. Without this SQLAlchemy
        # defaults to pool_size=5 + max_overflow=10 = 15 connections/process,
        # which the single-worker uvicorn API exhausts under Mini App
        # open-bursts (each open fires several parallel API calls). uvicorn
        # raises pool_size/max_overflow via its systemd env; pollers and the
        # job-runner keep the modest default. pool_pre_ping avoids handing out
        # connections dropped by a server-side/idle timeout.
        engine_kwargs["pool_size"] = settings.db_pool_size
        engine_kwargs["max_overflow"] = settings.db_max_overflow
        engine_kwargs["pool_timeout"] = settings.db_pool_timeout
        engine_kwargs["pool_pre_ping"] = settings.db_pool_pre_ping

    engine = create_engine(database_url, **engine_kwargs)
    if database_url.startswith("sqlite"):
        _configure_sqlite_engine(engine)
    return engine


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)


def get_db_session() -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


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
