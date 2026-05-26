"""SQLite Alembic round-trip for migration 0051 (Sub-A10, Group 3.1).

Same shape as the 0050 round-trip test: bootstrap the full current
schema via ``Base.metadata.create_all``, stamp Alembic at revision
0051, then downgrade to 0050 + upgrade back. Verifies that the
``batch_alter_table`` rebuilds work for both directions on SQLite
across all five target tables.
"""

from __future__ import annotations

import pytest
from alembic.config import Config
from alembic import command
from pathlib import Path

from sqlalchemy import create_engine, inspect


PROJECT_ROOT = Path(__file__).resolve().parents[2]


_TARGET_TABLES = (
    "shopping_list_items",
    "family_reminders",
    "tasks_items",
    "recipes",
    "checklists",
)


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


def _bootstrap_full_schema_stamped_at_0051(engine, cfg) -> None:
    from sreda.db.base import Base
    import sreda.db.models  # noqa: F401
    import sreda.db.models.audit  # noqa: F401
    import sreda.db.models.checklists  # noqa: F401
    import sreda.db.models.free_tier  # noqa: F401
    import sreda.db.models.reply_buttons  # noqa: F401

    Base.metadata.create_all(engine)
    command.stamp(cfg, "20260526_0051")


def test_migration_0051_downgrade_then_upgrade(alembic_cfg):
    """Codex-style migration smoke for 0051. Downgrade removes the
    new columns + indexes from all five tables; upgrade re-creates them."""
    cfg, db_path = alembic_cfg
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    _bootstrap_full_schema_stamped_at_0051(engine, cfg)

    # Downgrade.
    command.downgrade(cfg, "20260526_0050")

    inspector = inspect(engine)
    for table in _TARGET_TABLES:
        cols = {c["name"] for c in inspector.get_columns(table)}
        assert "operation_id" not in cols, (
            f"downgrade did not remove {table}.operation_id; got {sorted(cols)}"
        )
        assert "normalized_title_hash" not in cols, (
            f"downgrade did not remove {table}.normalized_title_hash"
        )

    # Upgrade back.
    command.upgrade(cfg, "20260526_0051")

    inspector = inspect(engine)
    for table in _TARGET_TABLES:
        cols = {c["name"] for c in inspector.get_columns(table)}
        assert "operation_id" in cols, (
            f"upgrade did not re-add {table}.operation_id; got {sorted(cols)}"
        )
        assert "normalized_title_hash" in cols, (
            f"upgrade did not re-add {table}.normalized_title_hash"
        )
        idx_names = {ix["name"] for ix in inspector.get_indexes(table)}
        assert f"ix_{table}_operation_id" in idx_names, (
            f"missing idempotency index on {table}; got {sorted(idx_names)}"
        )
        assert f"ix_{table}_normalized_title" in idx_names, (
            f"missing dedup index on {table}"
        )

    engine.dispose()
