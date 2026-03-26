from pathlib import Path

from sreda.config.settings import get_settings
from sreda.db.session import get_engine, get_session_factory


def test_sqlite_engine_sets_wal_and_busy_timeout(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    engine = get_engine()
    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()

    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) == 30000
    assert int(foreign_keys) == 1
