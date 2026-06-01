"""SQLite Alembic round-trip for migration 0054 (Sub-A12 Phase E PR-2a).

Same shape as the 0053 round-trip test: bootstrap the full current schema
via ``Base.metadata.create_all``, stamp Alembic at 0054 (the new head),
downgrade to 0053, verify the new tables/columns are gone, then upgrade
back and confirm everything is re-created.

New tables verified:
    step_execution_ledger
    planner_llm_reservations

New columns on planner_executions verified:
    recovery_worker_id, recovery_attempt, recovery_lease_until
    turn_key
    failure_kind, failure_stage, retryable,
    step_count, unknown_outcome_count, write_committed
"""

from __future__ import annotations

import pytest
from alembic.config import Config
from alembic import command
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


PROJECT_ROOT = Path(__file__).resolve().parents[2]

_NEW_TABLES = ("step_execution_ledger", "planner_llm_reservations")

_NEW_EXEC_COLS = (
    "recovery_worker_id",
    "recovery_attempt",
    "recovery_lease_until",
    "turn_key",
    "failure_kind",
    "failure_stage",
    "retryable",
    "step_count",
    "unknown_outcome_count",
    "write_committed",
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


def _bootstrap_full_schema_stamped_at_0054(engine, cfg) -> None:
    """Create the full current schema and stamp Alembic at 0054.

    Follows the same pattern as the 0053 bootstrap helper:
    create_all reflects current ORM models (which already include all new
    columns/tables), stamp at the head revision so Alembic's migration
    graph is anchored correctly.
    """
    from sreda.db.base import Base
    import sreda.db.models  # noqa: F401
    import sreda.db.models.audit  # noqa: F401
    import sreda.db.models.checklists  # noqa: F401
    import sreda.db.models.free_tier  # noqa: F401
    import sreda.db.models.reply_buttons  # noqa: F401

    Base.metadata.create_all(engine)
    command.stamp(cfg, "20260601_0054")


def test_migration_0054_downgrade_then_upgrade(alembic_cfg):
    """Alembic smoke for 0054: downgrade to 0053 removes new tables and
    new planner_executions columns; upgrade re-creates them."""
    cfg, db_path = alembic_cfg
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    _bootstrap_full_schema_stamped_at_0054(engine, cfg)

    # --- downgrade to 0053 ---
    command.downgrade(cfg, "20260601_0053")

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    for t in _NEW_TABLES:
        assert t not in tables, (
            f"downgrade did not drop table {t}; tables={sorted(tables)}"
        )

    exec_cols = {c["name"] for c in inspector.get_columns("planner_executions")}
    for col in _NEW_EXEC_COLS:
        assert col not in exec_cols, (
            f"downgrade did not remove planner_executions.{col}; "
            f"cols={sorted(exec_cols)}"
        )

    # --- upgrade back to 0054 ---
    command.upgrade(cfg, "20260601_0054")

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    for t in _NEW_TABLES:
        assert t in tables, (
            f"upgrade did not re-create table {t}; tables={sorted(tables)}"
        )

    exec_cols = {c["name"] for c in inspector.get_columns("planner_executions")}
    for col in _NEW_EXEC_COLS:
        assert col in exec_cols, (
            f"upgrade did not re-add planner_executions.{col}; "
            f"cols={sorted(exec_cols)}"
        )

    # Spot-check: expected indexes on the new tables exist.
    assert "ix_step_execution_ledger_execution_id" in {
        ix["name"] for ix in inspector.get_indexes("step_execution_ledger")
    }
    assert "ix_planner_llm_reservations_execution_id" in {
        ix["name"] for ix in inspector.get_indexes("planner_llm_reservations")
    }
    assert "ix_planner_executions_recovery_lease" in {
        ix["name"] for ix in inspector.get_indexes("planner_executions")
    }

    engine.dispose()


def _insert_exec(conn, exec_id: str, status: str) -> None:
    """Insert a minimal planner_executions row with the given execution_status.

    Caller must have already disabled FK checks (PRAGMA foreign_keys = OFF)
    on the connection so that missing FK parents don't interfere.
    """
    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(text(
        "INSERT INTO planner_executions "
        "(id, run_id, tenant_id, feature_key, planner_prompt_version, "
        " planner_provider, planner_model, planner_status, "
        " execution_status, execution_log_json, created_at) "
        "VALUES (:id, 'run_dummy', 't1', 'feat', 1, 'mimo', 'mimo', "
        "        'pending', :status, '[]', :now)"
    ), {"id": exec_id, "status": status, "now": now_iso})


def test_migration_0054_execution_status_check_constraint(alembic_cfg):
    """After upgrading to 0054 the execution_status CHECK accepts both new
    terminal values (failed_needs_manual, committed_unserved_needs_manual)
    and rejects bogus values.

    Strategy: bootstrap full schema at 0054, then exercise the CHECK via
    raw SQL inserts with PRAGMA foreign_keys = OFF so we can insert into
    planner_executions without seeding the full FK parent chain.
    This isolates the CHECK constraint test from unrelated table schemas.
    """
    cfg, db_path = alembic_cfg
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    _bootstrap_full_schema_stamped_at_0054(engine, cfg)

    # New terminal values must be accepted by the CHECK.
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        _insert_exec(conn, "pe_fnm", "failed_needs_manual")
        _insert_exec(conn, "pe_cun", "committed_unserved_needs_manual")

    # A bogus status must be rejected by the CHECK even with FK off.
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys = OFF"))
            _insert_exec(conn, "pe_bad", "totally_bogus_status")

    engine.dispose()


def test_migration_0054_migrated_check_accepts_new_statuses(alembic_cfg):
    """Prove that the CHECK installed by migration 0054's batch drop/recreate
    (not just create_all metadata) accepts the new terminal statuses.

    This goes beyond the create_all-based test above: it actually exercises
    the constraint that the migration's upgrade() installs via
    batch_alter_table drop_constraint + create_check_constraint.

    Strategy: bootstrap at 0054, downgrade to 0053 (removes new columns +
    restores old CHECK), then upgrade back to 0054 (reinstalls the new CHECK
    via batch_alter_table).  AFTER that round-trip, insert rows with both new
    terminal statuses and assert they succeed; insert a bogus status and assert
    IntegrityError.  PRAGMA foreign_keys = OFF avoids FK-parent seed noise.
    """
    cfg, db_path = alembic_cfg
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    _bootstrap_full_schema_stamped_at_0054(engine, cfg)

    # Downgrade to 0053 — removes new columns, restores old CHECK.
    command.downgrade(cfg, "20260601_0053")

    # Upgrade back to 0054 — this is the path under test: the migration's
    # batch_alter_table drop/recreate installs the new CHECK.
    command.upgrade(cfg, "20260601_0054")

    # New terminal statuses must be accepted by the migrated CHECK.
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        _insert_exec(conn, "mt_fnm", "failed_needs_manual")
        _insert_exec(conn, "mt_cun", "committed_unserved_needs_manual")

    # A bogus status must be rejected by the migrated CHECK.
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys = OFF"))
            _insert_exec(conn, "mt_bad", "bogus_after_migration")

    engine.dispose()
