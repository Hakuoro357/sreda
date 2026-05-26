"""SQLite Alembic round-trip for migration 0052 (Sub-A11, Category I).

Same shape as the 0050 / 0051 round-trip tests: bootstrap the full
current schema, stamp at 0052, downgrade to 0051, verify cleanup,
upgrade back, verify re-create.

Pattern documented in tests/unit/test_alembic_conversation_turns_sqlite.py.
"""

from __future__ import annotations

import pytest
from alembic.config import Config
from alembic import command
from pathlib import Path

from sqlalchemy import create_engine, inspect


PROJECT_ROOT = Path(__file__).resolve().parents[2]


_NEW_TABLES = ("user_data_change_feed", "audit_outbox")


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
    db_path = tmp_path / "test_migrations.sqlite"
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("SREDA_DATABASE_URL", url)
    monkeypatch.setenv("SREDA_LANGGRAPH_CHECKPOINTER", "memory")
    monkeypatch.delenv("SREDA_LANGGRAPH_PERSISTENCE_OPTED_IN", raising=False)

    from sreda.config.settings import get_settings
    get_settings.cache_clear()

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    yield cfg, db_path
    get_settings.cache_clear()


def _bootstrap_full_schema_stamped_at_0052(engine, cfg) -> None:
    from sreda.db.base import Base
    import sreda.db.models  # noqa: F401
    import sreda.db.models.audit  # noqa: F401
    import sreda.db.models.checklists  # noqa: F401
    import sreda.db.models.free_tier  # noqa: F401
    import sreda.db.models.reply_buttons  # noqa: F401

    Base.metadata.create_all(engine)
    command.stamp(cfg, "20260526_0052")


def test_migration_0052_downgrade_then_upgrade(alembic_cfg):
    cfg, db_path = alembic_cfg
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    _bootstrap_full_schema_stamped_at_0052(engine, cfg)

    command.downgrade(cfg, "20260526_0051")

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for t in _NEW_TABLES:
        assert t not in tables, (
            f"downgrade did not drop {t}; tables={sorted(tables)}"
        )

    command.upgrade(cfg, "20260526_0052")

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for t in _NEW_TABLES:
        assert t in tables, (
            f"upgrade did not re-create {t}; tables={sorted(tables)}"
        )
        # Confirm the recency index exists.
        names = {ix["name"] for ix in inspector.get_indexes(t)}
        assert f"ix_{t}_tenant_recent" in names, (
            f"missing recency index on {t}; got {names}"
        )

    engine.dispose()
