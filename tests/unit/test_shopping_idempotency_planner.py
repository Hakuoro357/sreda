"""Planner-path idempotency tests for HousewifeShoppingService.add_items.

Sub-A12 Phase E PR-2a — option-(a) adapter:
  - an inserted row gets a non-null PER-ITEM operation_id + normalized_title_hash
  - exactly ONE AuditOutboxEvent per step, keyed by the PER-STEP
    ``ctx.operation_id`` (the recovery probe surface), exists after commit
  - a SECOND add_items of the SAME item under the SAME ctx does NOT create a
    duplicate row and does NOT create a duplicate audit event
  - ON CONFLICT idempotency is exercised directly (bypassing the title-dedup by
    marking the first row bought) — the per-item op_id, not the title, is the key
  - a title-deduped step STILL emits its own per-step audit footprint, so no
    durable-write step is invisible to crash recovery
  - normalized_title_hash is scoped to the ``user_id`` ARGUMENT, never the
    (currently always-None) ``ctx.user_id``
  - a tenant-boundary leak in the runtime context fails closed
  - the Core ``INSERT`` stores the title ENCRYPTED at rest
  - the registry marker has_idempotency_adapter is True on add_shopping_items
    and False on a read tool (list_shopping).

Uses in-memory SQLite.  The unique index on (tenant_id, user_id, operation_id)
works on SQLite so ON CONFLICT DO NOTHING is exercised end-to-end.
"""

from __future__ import annotations

import base64

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.audit_feed import AuditOutboxEvent
from sreda.db.models.core import Tenant, User
from sreda.db.models.housewife_food import ShoppingListItem
from sreda.runtime.planner.tool_runtime import ToolRuntimeContext, bind_tool_runtime
from sreda.services.encryption import get_encryption_service
from sreda.services.housewife_shopping import HousewifeShoppingService
from sreda.services.operation_id import compute_normalized_title_hash


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stable_encryption_key(monkeypatch):
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY_ID", "test")
    monkeypatch.delenv("SREDA_ENCRYPTION_KEY_SALT", raising=False)
    monkeypatch.delenv("SREDA_ENCRYPTION_LEGACY_KEYS", raising=False)
    from sreda.config.settings import get_settings

    get_settings.cache_clear()
    get_encryption_service.cache_clear()
    yield
    get_settings.cache_clear()
    get_encryption_service.cache_clear()


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="Test"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    sess.commit()
    yield sess
    sess.close()


def _make_ctx(
    *,
    execution_id: str = "exec_abc123",
    step_id: str = "step_1",
    operation_id: str | None = None,
    tenant_id: str = "t1",
) -> ToolRuntimeContext:
    """Minimal ToolRuntimeContext for shopping tests.

    ``operation_id`` here is the PER-STEP ledger id — the recovery probe
    surface that the per-step audit event is keyed by.  The tool derives its
    own PER-ITEM op_id (hash including the title) for the row uniqueness key.

    ``user_id`` is set to ``None`` deliberately — that mirrors what the real
    executor binds (executor.py:553).  It proves the row + hash use the
    explicit ``user_id`` ARGUMENT, not ``ctx.user_id``.
    """
    return ToolRuntimeContext(
        operation_id=operation_id or f"op_{execution_id}_{step_id}",
        execution_id=execution_id,
        step_id=step_id,
        tool_name="add_shopping_items",
        tenant_id=tenant_id,
        user_id=None,
    )


def _raw_title(session, row_id: str) -> str:
    """Read the raw stored ``title`` column, bypassing the ORM type decorator,
    so we can assert it is encrypted at rest."""
    return session.execute(
        text("SELECT title FROM shopping_list_items WHERE id = :id"),
        {"id": row_id},
    ).scalar_one()


# ---------------------------------------------------------------------------
# Core planner-path assertions
# ---------------------------------------------------------------------------


def test_planner_path_sets_operation_id_and_title_hash(session):
    """Row created under planner ctx must have non-null operation_id and
    normalized_title_hash."""
    ctx = _make_ctx()
    svc = HousewifeShoppingService(session)
    with bind_tool_runtime(ctx):
        rows = svc.add_items(
            tenant_id="t1",
            user_id="u1",
            items=[{"title": "молоко", "category": "молочные"}],
        )

    assert len(rows) == 1
    row = rows[0]
    session.expire_all()
    row = session.get(ShoppingListItem, row.id)
    assert row.operation_id is not None, "operation_id must be set in planner path"
    assert row.operation_id.startswith("op_"), (
        f"operation_id shape wrong: {row.operation_id!r}"
    )
    assert row.normalized_title_hash is not None, (
        "normalized_title_hash must be set in planner path"
    )
    assert len(row.normalized_title_hash) == 64, (
        f"normalized_title_hash should be 64-char hex, got {row.normalized_title_hash!r}"
    )


def test_title_hash_scoped_to_user_argument_not_ctx_user(session):
    """normalized_title_hash must be scoped to the ``user_id`` ARGUMENT, not
    ``ctx.user_id`` (which the executor binds to None).  Codex A/B R1 MAJOR:
    the old code fell back to ``""`` when ctx.user_id was None, scoping the
    hash to a blank user."""
    ctx = _make_ctx()
    assert ctx.user_id is None, "fixture must mirror the executor's None binding"
    svc = HousewifeShoppingService(session)
    with bind_tool_runtime(ctx):
        rows = svc.add_items(
            tenant_id="t1",
            user_id="u1",
            items=[{"title": "кефир", "category": "молочные"}],
        )

    expected = compute_normalized_title_hash(
        "кефир",
        entity_type="shopping_list_item",
        tenant_id="t1",
        user_id="u1",
    )
    assert rows[0].normalized_title_hash == expected, (
        "hash must be scoped to the real user_id argument, not blank/ctx.user_id"
    )
    # Sanity: a blank-user-scoped hash (the old buggy value) must DIFFER, else
    # the assertion above proves nothing.
    blank = compute_normalized_title_hash(
        "кефир",
        entity_type="shopping_list_item",
        tenant_id="t1",
        user_id="",
    )
    assert expected != blank, "test premise broken: blank-user hash must differ"


def test_planner_path_audit_keyed_by_ctx_operation_id(session):
    """The single per-step AuditOutboxEvent must be keyed by the PER-STEP
    ``ctx.operation_id`` (recovery probe surface) — NOT the per-item row op_id.

    This is the Codex A/B R1 CRITICAL: recovery.probe_operation() looks up the
    ledger ``ctx.operation_id``; if the audit were keyed by the per-item id, a
    committed durable write would be invisible to recovery.
    """
    ctx = _make_ctx()
    svc = HousewifeShoppingService(session)
    with bind_tool_runtime(ctx):
        rows = svc.add_items(
            tenant_id="t1",
            user_id="u1",
            items=[{"title": "хлеб", "category": "хлеб"}],
        )

    assert len(rows) == 1
    row_op_id = rows[0].operation_id
    assert row_op_id is not None

    # Exactly one audit event, keyed by ctx.operation_id.
    audits = (
        session.query(AuditOutboxEvent)
        .filter(AuditOutboxEvent.operation_id == ctx.operation_id)
        .all()
    )
    assert len(audits) == 1, (
        f"expected exactly one per-step audit keyed by ctx.operation_id="
        f"{ctx.operation_id!r}, got {len(audits)}"
    )
    audit = audits[0]
    assert audit.entity_type == "shopping_list_item"
    assert audit.action == "created"
    assert audit.tenant_id == "t1"
    assert audit.user_id == "u1"  # the ARGUMENT, not ctx.user_id (None)
    assert audit.entity_id is None, "per-step footprint has no single entity_id"

    # The per-ITEM row op_id must NOT be used as an audit key.
    assert row_op_id != ctx.operation_id, (
        "row op_id (per-item) and ctx.operation_id (per-step) must differ"
    )
    stray = (
        session.query(AuditOutboxEvent)
        .filter(AuditOutboxEvent.operation_id == row_op_id)
        .count()
    )
    assert stray == 0, "no audit should be keyed by the per-item row op_id"


def test_planner_path_idempotent_on_retry_same_ctx(session):
    """A second add_items call with the same ctx + same item title must NOT
    create a duplicate row and must NOT create a duplicate per-step audit.
    """
    ctx = _make_ctx()
    svc = HousewifeShoppingService(session)

    with bind_tool_runtime(ctx):
        rows1 = svc.add_items(
            tenant_id="t1",
            user_id="u1",
            items=[{"title": "яйца", "category": "другое"}],
        )
    assert len(rows1) == 1

    # Second call — same ctx, same title (simulates a retry while pending).
    with bind_tool_runtime(ctx):
        rows2 = svc.add_items(
            tenant_id="t1",
            user_id="u1",
            items=[{"title": "яйца", "category": "другое"}],
        )

    # No duplicate row.
    total_rows = session.query(ShoppingListItem).filter_by(
        tenant_id="t1", user_id="u1", status="pending"
    ).count()
    assert total_rows == 1, f"Expected 1 shopping row after retry, got {total_rows}"

    # No duplicate per-step audit (keyed by ctx.operation_id).
    total_audit = session.query(AuditOutboxEvent).filter_by(
        operation_id=ctx.operation_id
    ).count()
    assert total_audit == 1, f"Expected 1 audit event after retry, got {total_audit}"

    # Retry still returns the existing row (planner can read its id).
    assert rows1[0].id in {r.id for r in rows2}, (
        "Retry should return the existing row's id"
    )


def test_on_conflict_idempotency_bypassing_title_dedup(session):
    """Exercise the DB ON CONFLICT directly, NOT the title pre-filter.

    Create an item, mark it bought (so the pending-title dedup no longer
    matches it), then retry with the SAME ctx + same title.  The per-item
    op_id is identical, so ``INSERT ... ON CONFLICT DO NOTHING`` fires and no
    duplicate row is created (Codex A/B R1 MAJOR: the prior retry test only
    exercised app-level title dedup).
    """
    ctx = _make_ctx(step_id="step_x")
    svc = HousewifeShoppingService(session)

    with bind_tool_runtime(ctx):
        rows1 = svc.add_items(
            tenant_id="t1",
            user_id="u1",
            items=[{"title": "масло", "category": "молочные"}],
        )
    assert len(rows1) == 1
    row_op_id = rows1[0].operation_id

    # Mark bought → title dedup (pending-only) will NOT skip the retry.
    session.query(ShoppingListItem).filter_by(id=rows1[0].id).update(
        {"status": "bought"}
    )
    session.commit()

    with bind_tool_runtime(ctx):
        rows2 = svc.add_items(
            tenant_id="t1",
            user_id="u1",
            items=[{"title": "масло", "category": "молочные"}],
        )

    # ON CONFLICT must have suppressed the insert: still exactly one row with
    # that per-item op_id, and it is the original (bought) row.
    rows_with_op = (
        session.query(ShoppingListItem)
        .filter_by(operation_id=row_op_id)
        .all()
    )
    assert len(rows_with_op) == 1, (
        f"ON CONFLICT must suppress the duplicate insert; got {len(rows_with_op)} rows"
    )
    assert rows_with_op[0].id == rows1[0].id
    assert rows_with_op[0].status == "bought"
    # The retry returns the conflicted (existing) row by op_id.
    assert rows1[0].id in {r.id for r in rows2}
    # No second pending row got created.
    pending = session.query(ShoppingListItem).filter_by(
        tenant_id="t1", user_id="u1", status="pending"
    ).count()
    assert pending == 0


def test_deduped_step_still_emits_its_own_audit(session):
    """A DIFFERENT step adding an already-pending title is title-deduped (no
    new row) BUT must still emit its OWN per-step audit footprint, so the step
    is visible to crash recovery (Codex A/B R1 CRITICAL #2 resolution).
    """
    svc = HousewifeShoppingService(session)
    ctx1 = _make_ctx(step_id="step_1")
    ctx2 = _make_ctx(step_id="step_2")
    assert ctx1.operation_id != ctx2.operation_id

    with bind_tool_runtime(ctx1):
        rows1 = svc.add_items(
            tenant_id="t1",
            user_id="u1",
            items=[{"title": "сахар", "category": "бакалея"}],
        )
    assert len(rows1) == 1

    # step_2 adds the SAME (still-pending) title → title-deduped, no new row.
    with bind_tool_runtime(ctx2):
        rows2 = svc.add_items(
            tenant_id="t1",
            user_id="u1",
            items=[{"title": "сахар", "category": "бакалея"}],
        )

    pending = session.query(ShoppingListItem).filter_by(
        tenant_id="t1", user_id="u1", status="pending"
    ).count()
    assert pending == 1, "title dedup must prevent a second pending row"
    # step_2 returns the existing row (planner sees an id).
    assert rows1[0].id in {r.id for r in rows2}

    # Both steps have their own audit footprint → recovery can probe either.
    assert (
        session.query(AuditOutboxEvent).filter_by(operation_id=ctx1.operation_id).count()
        == 1
    )
    assert (
        session.query(AuditOutboxEvent).filter_by(operation_id=ctx2.operation_id).count()
        == 1
    ), "the title-deduped step must STILL emit its own audit (recovery visibility)"


def test_planner_path_different_step_id_creates_new_row(session):
    """A different step_id produces a different PER-ITEM operation_id → a new
    row is created (not de-duped by ON CONFLICT) once the title is no longer
    pending."""
    ctx1 = _make_ctx(step_id="step_1")
    ctx2 = _make_ctx(step_id="step_2")
    svc = HousewifeShoppingService(session)

    with bind_tool_runtime(ctx1):
        rows1 = svc.add_items(
            tenant_id="t1",
            user_id="u1",
            items=[{"title": "сыр", "category": "молочные"}],
        )

    # Mark step_1's row bought so the pending-title dedup doesn't block step_2.
    session.query(ShoppingListItem).filter_by(id=rows1[0].id).update(
        {"status": "bought"}
    )
    session.commit()

    with bind_tool_runtime(ctx2):
        rows2 = svc.add_items(
            tenant_id="t1",
            user_id="u1",
            items=[{"title": "сыр", "category": "молочные"}],
        )

    assert len(rows2) == 1
    assert rows2[0].id != rows1[0].id, "Different step_id should produce a new row"
    assert rows2[0].operation_id != rows1[0].operation_id


def test_tenant_boundary_leak_fails_closed(session):
    """A ctx whose tenant_id does not match the call's tenant_id is a wiring
    leak — the planner path must refuse rather than write cross-tenant rows."""
    ctx = _make_ctx(tenant_id="other_tenant")
    svc = HousewifeShoppingService(session)
    with bind_tool_runtime(ctx):
        with pytest.raises(ValueError, match="tenant boundary"):
            svc.add_items(
                tenant_id="t1",
                user_id="u1",
                items=[{"title": "молоко", "category": "молочные"}],
            )


def test_core_insert_stores_encrypted_title(session):
    """The planner Core INSERT must store the title ENCRYPTED at rest (the
    EncryptedString TypeDecorator must run for Core inserts, not just ORM
    add()).  Codex A/B R1 MINOR — first security-sensitive wiring."""
    ctx = _make_ctx()
    svc = HousewifeShoppingService(session)
    with bind_tool_runtime(ctx):
        rows = svc.add_items(
            tenant_id="t1",
            user_id="u1",
            items=[{"title": "творог", "category": "молочные"}],
        )
    row_id = rows[0].id

    raw = _raw_title(session, row_id)
    assert raw != "творог", "title stored as plaintext — encryption did not run"
    assert "творог" not in raw, "plaintext leaked into the stored ciphertext"

    # ORM read still round-trips to plaintext.
    session.expire_all()
    assert session.get(ShoppingListItem, row_id).title == "творог"


# ---------------------------------------------------------------------------
# Registry marker assertion
# ---------------------------------------------------------------------------


def _tool_registry() -> dict:
    """Build a name→ToolSpec dict from the migrated specs list."""
    from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS
    return {s.name: s for s in MIGRATED_TOOL_SPECS}


def test_registry_marker_add_shopping_items_true():
    """add_shopping_items ToolSpec must have has_idempotency_adapter=True."""
    registry = _tool_registry()
    spec = registry["add_shopping_items"]
    assert spec.has_idempotency_adapter is True, (
        "add_shopping_items must have has_idempotency_adapter=True "
        "(Sub-A12 Phase E PR-2a wiring)"
    )


def test_registry_marker_list_shopping_false():
    """A read tool (list_shopping) must have has_idempotency_adapter=False
    (it has no durable-write path to adapt)."""
    registry = _tool_registry()
    spec = registry["list_shopping"]
    assert spec.has_idempotency_adapter is False, (
        "list_shopping must NOT have has_idempotency_adapter=True"
    )
