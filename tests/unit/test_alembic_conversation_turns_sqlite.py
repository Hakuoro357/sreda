"""SQLite Alembic upgrade/downgrade smoke for Sub-A9 (Codex R2 MAJOR #3).

R2 review flagged that the SQLite path through the 0050 migration
wasn't actually exercised — unit tests use ``Base.metadata.create_all()``
which bypasses Alembic. This test drives the real migration via the
Alembic command API on an in-memory SQLite engine, then verifies the
schema before/after.

What this catches:
  - ``batch_alter_table`` rebuild bugs (composite FK on SQLite).
  - Downgrade dropping renamed/reflected constraints.
  - Migration / model schema drift.

We use the project's actual alembic.ini, override the URL to in-memory
SQLite, and run ``upgrade head`` followed by ``downgrade -1``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic.config import Config
from alembic import command
from sqlalchemy import create_engine, inspect


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
    """Build an Alembic Config pointing at a fresh on-disk SQLite file.

    On-disk (not ``:memory:``) because Alembic opens a separate
    connection inside the migration and a per-process ``:memory:``
    handle wouldn't share state.

    The project's ``migrations/env.py`` reads the URL from
    ``sreda.config.settings.get_settings().database_url`` (line 31),
    overriding whatever we set on the Config object. Workaround:
    set ``SREDA_DATABASE_URL`` env var + clear settings cache so the
    in-test get_settings() returns our SQLite URL.
    """
    db_path = tmp_path / "test_migrations.sqlite"
    url = f"sqlite:///{db_path.as_posix()}"

    monkeypatch.setenv("SREDA_DATABASE_URL", url)
    # Force the SQLite-compatible langgraph checkpointer for the
    # settings used during migration (avoids any side path that might
    # peek at postgres-only settings).
    monkeypatch.setenv("SREDA_LANGGRAPH_CHECKPOINTER", "memory")
    monkeypatch.delenv("SREDA_LANGGRAPH_PERSISTENCE_OPTED_IN", raising=False)

    from sreda.config.settings import get_settings
    get_settings.cache_clear()

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option(
        "script_location", str(PROJECT_ROOT / "migrations")
    )
    yield cfg, db_path
    get_settings.cache_clear()


def _bootstrap_full_schema_stamped_at_0050(engine, cfg) -> None:
    """Materialize the full current schema (post-0050) via ORM
    ``create_all`` + stamp Alembic at revision 0050.

    Why not run the full migration chain? Many historical migrations
    in this project use ``add_constraint`` outside ``batch_alter_table``
    which SQLite refuses. So instead of running them, we build the
    schema directly from the ORM models and tell Alembic we're at
    revision 0050. From there we can exercise the 0050↔0049 transition
    (which is the only one we wrote/modified) in both directions.
    """
    from sreda.db.base import Base
    # Register all models — mirror conftest.py's import ordering so
    # FK targets like `checklists` and `tasks_items` are present.
    import sreda.db.models  # noqa: F401
    import sreda.db.models.audit  # noqa: F401
    import sreda.db.models.checklists  # noqa: F401
    import sreda.db.models.free_tier  # noqa: F401
    import sreda.db.models.reply_buttons  # noqa: F401

    Base.metadata.create_all(engine)
    command.stamp(cfg, "20260526_0050")


def test_migration_0050_downgrade_then_upgrade(alembic_cfg):
    """Codex Sub-A9 R2 MAJOR #3 — the heart of the SQLite migration
    safety check.

    Steps:
      1. Bootstrap full current schema + stamp at 0050.
      2. ``downgrade 0050 -> 0049``: the migration's batch_alter_table
         rebuild + table drop must succeed on SQLite.
      3. Verify ``conversation_turns`` and ``agent_runs.turn_id`` are
         gone.
      4. ``upgrade 0049 -> 0050``: the migration's batch add_column +
         composite FK creation must succeed on SQLite.
      5. Verify the new objects re-materialise.

    If any step trips a SQLite-incompatible ALTER (no batch wrapping,
    FK-name reflection issue on downgrade, partial-index syntax
    quirk), this fails."""
    cfg, db_path = alembic_cfg
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    _bootstrap_full_schema_stamped_at_0050(engine, cfg)

    # Step 2: downgrade.
    command.downgrade(cfg, "20260526_0049")

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "conversation_turns" not in tables, (
        f"downgrade did not drop conversation_turns; tables={sorted(tables)}"
    )
    agent_runs_cols = {c["name"] for c in inspector.get_columns("agent_runs")}
    assert "turn_id" not in agent_runs_cols, (
        f"downgrade did not remove agent_runs.turn_id; "
        f"columns={sorted(agent_runs_cols)}"
    )

    # Step 4: upgrade back.
    command.upgrade(cfg, "20260526_0050")

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "conversation_turns" in tables, (
        f"upgrade did not re-create conversation_turns; tables={sorted(tables)}"
    )
    agent_runs_cols = {c["name"] for c in inspector.get_columns("agent_runs")}
    assert "turn_id" in agent_runs_cols, (
        f"upgrade did not re-add agent_runs.turn_id; "
        f"columns={sorted(agent_runs_cols)}"
    )

    # Spot-check the partial unique index re-created cleanly.
    conv_indexes = inspector.get_indexes("conversation_turns")
    conv_index_names = {ix["name"] for ix in conv_indexes}
    assert "ix_one_active_turn_per_thread" in conv_index_names, (
        f"partial unique missing after upgrade; got={conv_index_names}"
    )

    engine.dispose()
