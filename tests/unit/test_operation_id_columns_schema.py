"""Schema-introspection tests for Sub-A10 (Group 3.1).

Verifies that the five user-facing tables touched by migration
``20260526_0051_operation_id_columns`` actually carry the new
``operation_id`` + ``normalized_title_hash`` columns AND have the
right partial indexes wired.

The tests run against the SQLite test DB built by ``db_session``
fixture, which uses ``Base.metadata.create_all`` — so they're
checking ORM model definitions, not the migration directly (the
Alembic round-trip test for 0050 covers the SQLite migration path
explicitly; we'd add a 0051-specific one if a regression surfaced).
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session


# Tables and the matching ORM classes — we cross-reference both
# the table name (for inspector lookups) and the model so a typo
# in either side fails the test loudly.
_TARGET_TABLES = (
    "shopping_list_items",
    "family_reminders",
    "tasks_items",
    "recipes",
    "checklists",
)


@pytest.mark.parametrize("table", _TARGET_TABLES)
def test_operation_id_column_exists(db_session: Session, table: str) -> None:
    inspector = inspect(db_session.bind)
    columns = {c["name"]: c for c in inspector.get_columns(table)}
    assert "operation_id" in columns, (
        f"{table} missing operation_id column; "
        f"columns={sorted(columns)}"
    )
    assert columns["operation_id"]["nullable"] is True, (
        f"{table}.operation_id must be nullable (legacy rows leave it NULL)"
    )


@pytest.mark.parametrize("table", _TARGET_TABLES)
def test_normalized_title_hash_column_exists(
    db_session: Session, table: str
) -> None:
    inspector = inspect(db_session.bind)
    columns = {c["name"]: c for c in inspector.get_columns(table)}
    assert "normalized_title_hash" in columns, (
        f"{table} missing normalized_title_hash column; "
        f"columns={sorted(columns)}"
    )
    assert columns["normalized_title_hash"]["nullable"] is True


@pytest.mark.parametrize("table", _TARGET_TABLES)
def test_operation_id_index_present(db_session: Session, table: str) -> None:
    """Partial UNIQUE index on (tenant_id, operation_id)."""
    inspector = inspect(db_session.bind)
    indexes = inspector.get_indexes(table)
    name = f"ix_{table}_operation_id"
    matching = [ix for ix in indexes if ix["name"] == name]
    assert matching, (
        f"{table} missing index {name!r}; got {[ix['name'] for ix in indexes]}"
    )
    ix = matching[0]
    # SQLite returns `unique: 1` instead of True — accept truthy.
    assert ix["unique"], (
        f"{name} must be UNIQUE for ON CONFLICT idempotency"
    )
    # Codex R1 MAJOR #3 — scope includes user_id.
    assert tuple(ix["column_names"]) == ("tenant_id", "user_id", "operation_id"), (
        f"{name} columns wrong: {ix['column_names']}"
    )


@pytest.mark.parametrize("table", _TARGET_TABLES)
def test_normalized_title_index_present(
    db_session: Session, table: str
) -> None:
    inspector = inspect(db_session.bind)
    indexes = inspector.get_indexes(table)
    name = f"ix_{table}_normalized_title"
    matching = [ix for ix in indexes if ix["name"] == name]
    assert matching, (
        f"{table} missing index {name!r}; got {[ix['name'] for ix in indexes]}"
    )
    ix = matching[0]
    # Non-unique — many rows can share the same lemma (think 5 shopping
    # items "молоко" added one per week).
    # Codex R1 MAJOR #3 — scope includes user_id.
    assert tuple(ix["column_names"]) == (
        "tenant_id",
        "user_id",
        "normalized_title_hash",
    ), f"{name} columns wrong: {ix['column_names']}"
