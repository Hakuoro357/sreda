"""SQLite Alembic round-trip for migration 0053 (PR-2a, encrypt planner PII).

Same shape as the 0051 / 0052 round-trip tests: bootstrap the full current
schema via ``Base.metadata.create_all``, stamp Alembic at 0053 (the current
head), ``downgrade`` to 0052 and confirm all 11 _enc columns are removed,
then ``upgrade`` back to 0053 and confirm they are re-added.

  planner_executions (6 new columns):
    raw_planner_response_enc, plan_json_enc, validation_errors_enc,
    execution_plan_json_enc, execution_log_json_enc,
    turn_classification_reason_enc

  planner_gaps (5 new columns):
    user_message_enc, plan_json_enc, tool_args_json_enc,
    actual_result_json_enc, error_details_enc
"""

from __future__ import annotations

import pytest
from alembic.config import Config
from alembic import command
from pathlib import Path

from sqlalchemy import create_engine, inspect


PROJECT_ROOT = Path(__file__).resolve().parents[2]

_EXECUTIONS_ENC_COLS = (
    "raw_planner_response_enc",
    "plan_json_enc",
    "validation_errors_enc",
    "execution_plan_json_enc",
    "execution_log_json_enc",
    "turn_classification_reason_enc",
)

_GAPS_ENC_COLS = (
    "user_message_enc",
    "plan_json_enc",
    "tool_args_json_enc",
    "actual_result_json_enc",
    "error_details_enc",
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


def _bootstrap_full_schema_stamped_at_0053(engine, cfg) -> None:
    """Create the full current schema (includes _enc columns) and stamp at
    0053 so Alembic knows we are already at the latest revision.

    This is the same pattern the 0051 / 0052 tests use: create_all reflects
    the current ORM models (which already include the new columns), stamp at
    the head revision so Alembic's migration graph is anchored correctly, then
    downgrade to remove the new columns and upgrade to re-add them.
    """
    from sreda.db.base import Base
    import sreda.db.models  # noqa: F401
    import sreda.db.models.audit  # noqa: F401
    import sreda.db.models.checklists  # noqa: F401
    import sreda.db.models.free_tier  # noqa: F401
    import sreda.db.models.reply_buttons  # noqa: F401

    Base.metadata.create_all(engine)
    command.stamp(cfg, "20260601_0053")


def test_migration_0053_downgrade_then_upgrade(alembic_cfg):
    """Alembic smoke for 0053: downgrade removes all 11 _enc columns from both
    tables; upgrade re-adds them.  Mirrors the shape of the 0051 test."""
    cfg, db_path = alembic_cfg
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    _bootstrap_full_schema_stamped_at_0053(engine, cfg)

    # Downgrade to 0052 — should drop all 11 _enc columns.
    command.downgrade(cfg, "20260526_0052")

    inspector = inspect(engine)

    exec_cols = {c["name"] for c in inspector.get_columns("planner_executions")}
    for col in _EXECUTIONS_ENC_COLS:
        assert col not in exec_cols, (
            f"downgrade did not remove planner_executions.{col}; got {sorted(exec_cols)}"
        )

    gap_cols = {c["name"] for c in inspector.get_columns("planner_gaps")}
    for col in _GAPS_ENC_COLS:
        assert col not in gap_cols, (
            f"downgrade did not remove planner_gaps.{col}; got {sorted(gap_cols)}"
        )

    # Upgrade back to 0053 — should re-add all 11 _enc columns.
    command.upgrade(cfg, "20260601_0053")

    inspector = inspect(engine)

    exec_cols = {c["name"] for c in inspector.get_columns("planner_executions")}
    for col in _EXECUTIONS_ENC_COLS:
        assert col in exec_cols, (
            f"upgrade did not re-add planner_executions.{col}; got {sorted(exec_cols)}"
        )

    gap_cols = {c["name"] for c in inspector.get_columns("planner_gaps")}
    for col in _GAPS_ENC_COLS:
        assert col in gap_cols, (
            f"upgrade did not re-add planner_gaps.{col}; got {sorted(gap_cols)}"
        )

    engine.dispose()
