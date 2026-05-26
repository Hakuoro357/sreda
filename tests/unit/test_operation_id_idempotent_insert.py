"""End-to-end idempotency test for the operation_id index (Codex R1 MINOR).

The point of ``operation_id`` columns + the matching unique indexes is
that a retried INSERT with the same op_id is a no-op. Codex R1 MINOR
flagged that we have schema-introspection tests but no test that
exercises the actual ``INSERT ... ON CONFLICT DO NOTHING`` SQL
against the new constraints.

This test does exactly that against the SQLite test DB:

  1. Insert a row with a specific operation_id → succeeds.
  2. Insert another row with the SAME (tenant_id, user_id, operation_id)
     and ``ON CONFLICT DO NOTHING`` → SQLite reports rowcount=0.
  3. Verify the original row is intact (not overwritten).
  4. Insert with a DIFFERENT user_id but same op_id → succeeds
     (different scope).

We use shopping_list_items as the canonical fixture; the same shape
applies to all five Sub-A10 tables.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _insert_shopping_with_op(
    session: Session,
    *,
    id: str,
    tenant_id: str,
    user_id: str,
    title: str,
    operation_id: str,
):
    """Plain ORM-style insert via raw SQL (avoids the EncryptedString
    column adapter — we don't care about encryption for the
    operation_id index test)."""
    session.execute(
        text(
            """
            INSERT INTO shopping_list_items (
                id, tenant_id, user_id, title, category, status,
                added_at, updated_at, operation_id
            ) VALUES (
                :id, :tenant_id, :user_id, :title, 'misc', 'pending',
                :added_at, :updated_at, :operation_id
            )
            ON CONFLICT (tenant_id, user_id, operation_id) DO NOTHING
            """
        ),
        {
            "id": id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "title": title,
            "added_at": _utcnow(),
            "updated_at": _utcnow(),
            "operation_id": operation_id,
        },
    )


def test_duplicate_op_id_same_user_is_no_op(db_session: Session) -> None:
    """Codex R1 MINOR — the unique constraint on (tenant_id, user_id,
    operation_id) is the foundation of idempotent retries. Two
    INSERTs with the same triple must produce exactly one row."""
    _insert_shopping_with_op(
        db_session,
        id="sh_first",
        tenant_id="t1",
        user_id="u1",
        title="молоко",
        operation_id="op_abc123",
    )
    _insert_shopping_with_op(
        db_session,
        id="sh_second",
        tenant_id="t1",
        user_id="u1",
        title="молоко",
        operation_id="op_abc123",
    )
    db_session.commit()

    rows = db_session.execute(
        text(
            "SELECT id FROM shopping_list_items WHERE operation_id=:op"
        ),
        {"op": "op_abc123"},
    ).scalars().all()
    assert rows == ["sh_first"], (
        f"second INSERT should have been a no-op; got rows={rows}"
    )


def test_same_op_id_different_user_both_succeed(db_session: Session) -> None:
    """Codex R1 MAJOR #3 — scope includes user_id, so two users in
    the same tenant can both have rows with the same op_id (e.g.
    common shopping items)."""
    _insert_shopping_with_op(
        db_session,
        id="sh_u1",
        tenant_id="t1",
        user_id="u1",
        title="молоко",
        operation_id="op_xyz",
    )
    _insert_shopping_with_op(
        db_session,
        id="sh_u2",
        tenant_id="t1",
        user_id="u2",
        title="молоко",
        operation_id="op_xyz",
    )
    db_session.commit()

    rows = sorted(
        db_session.execute(
            text("SELECT id FROM shopping_list_items WHERE operation_id=:op"),
            {"op": "op_xyz"},
        ).scalars().all()
    )
    assert rows == ["sh_u1", "sh_u2"]


def test_null_op_id_doesnt_collide(db_session: Session) -> None:
    """Legacy rows with operation_id=NULL must not collide on the
    unique constraint (standard SQL treats NULLs as distinct)."""
    _insert_shopping_with_op(
        db_session,
        id="sh_legacy_1",
        tenant_id="t1",
        user_id="u1",
        title="молоко",
        operation_id=None,
    )
    _insert_shopping_with_op(
        db_session,
        id="sh_legacy_2",
        tenant_id="t1",
        user_id="u1",
        title="хлеб",
        operation_id=None,
    )
    db_session.commit()

    rows = db_session.execute(
        text("SELECT id FROM shopping_list_items WHERE operation_id IS NULL")
    ).scalars().all()
    assert sorted(rows) == ["sh_legacy_1", "sh_legacy_2"]
