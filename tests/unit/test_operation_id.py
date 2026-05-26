"""Tests for ``sreda.services.operation_id`` (Sub-A10, Group 3.1).

The ``operation_id`` is a deterministic hash that lets us safely retry
a tool side-effect (e.g. add_shopping_items) without creating a
duplicate row on retry. Codex IDEA review R1 + plan Group 3.1:

  - For ``create`` operations: ``op_id = sha1(plan_id, step_id,
    action, entity_type, logical_key)`` where ``logical_key`` is the
    pre-INSERT canonical form (typically ``normalize_for_dedup(title)``).
    Retries with the same plan + step + title produce the same op_id
    → ``INSERT ... ON CONFLICT (tenant_id, user_id, operation_id)
    DO NOTHING`` is idempotent (full UNIQUE, not partial — Codex
    Sub-A10 R1 CRITICAL #1).
  - For ``update/delete``: ``op_id = sha1(plan_id, step_id, action,
    entity_type, entity_id)``. ``entity_id`` is known up front so
    retries with the same target produce the same op_id.

The ``normalized_title_hash`` is HMAC-SHA256 (keyed by
``SREDA_ENCRYPTION_KEY``) over a scoped payload of
(NORMALIZATION_VERSION, entity_type, tenant_id, user_id,
normalize_for_dedup(title)). Used for semantic-dedup lookups via
SQL ``WHERE normalized_title_hash = ?``. The column is unencrypted
so it can participate in indexes, but HMAC keying prevents both
plaintext recovery from dictionary attacks and cross-domain /
cross-tenant equality leakage. Codex R1 MAJOR #4 + R2 MAJOR #3.
"""

from __future__ import annotations

import pytest

from sreda.services.operation_id import (
    compute_normalized_title_hash,
    compute_operation_id_create,
    compute_operation_id_update,
)


# ---------------------------------------------------------------------------
# compute_operation_id_create
# ---------------------------------------------------------------------------


def test_create_op_id_deterministic():
    """Same inputs → same op_id."""
    a = compute_operation_id_create(
        plan_id="plan_001",
        step_id="s1",
        action="add",
        entity_type="shopping_list_item",
        logical_key="молоко",
    )
    b = compute_operation_id_create(
        plan_id="plan_001",
        step_id="s1",
        action="add",
        entity_type="shopping_list_item",
        logical_key="молоко",
    )
    assert a == b


def test_create_op_id_starts_with_op_prefix():
    """Format: ``op_<hex>`` so it's clearly recognizable in logs."""
    op_id = compute_operation_id_create(
        plan_id="plan_001",
        step_id="s1",
        action="add",
        entity_type="shopping_list_item",
        logical_key="молоко",
    )
    assert op_id.startswith("op_")
    assert len(op_id) > 10  # op_ + non-trivial hash


def test_create_op_id_changes_with_plan():
    """Different plan_id → different op_id (so first-message create vs
    second-message create produce distinct ops)."""
    a = compute_operation_id_create(
        plan_id="plan_001",
        step_id="s1",
        action="add",
        entity_type="shopping_list_item",
        logical_key="молоко",
    )
    b = compute_operation_id_create(
        plan_id="plan_002",
        step_id="s1",
        action="add",
        entity_type="shopping_list_item",
        logical_key="молоко",
    )
    assert a != b


def test_create_op_id_changes_with_logical_key():
    """Different logical_key (different normalized title) → different op_id."""
    a = compute_operation_id_create(
        plan_id="p",
        step_id="s",
        action="add",
        entity_type="shopping_list_item",
        logical_key="молоко",
    )
    b = compute_operation_id_create(
        plan_id="p",
        step_id="s",
        action="add",
        entity_type="shopping_list_item",
        logical_key="хлеб",
    )
    assert a != b


def test_create_op_id_changes_with_entity_type():
    """Same logical_key but different entity_type → different op_id.
    Prevents collisions between e.g. shopping item "урок" and a task "урок"."""
    a = compute_operation_id_create(
        plan_id="p",
        step_id="s",
        action="add",
        entity_type="shopping_list_item",
        logical_key="урок",
    )
    b = compute_operation_id_create(
        plan_id="p",
        step_id="s",
        action="add",
        entity_type="task",
        logical_key="урок",
    )
    assert a != b


# ---------------------------------------------------------------------------
# compute_operation_id_update
# ---------------------------------------------------------------------------


def test_update_op_id_uses_entity_id():
    """For update/delete entity_id is known upfront; same entity_id
    → same op_id on retry."""
    a = compute_operation_id_update(
        plan_id="plan_001",
        step_id="s2",
        action="update",
        entity_type="shopping_list_item",
        entity_id="sh_abc",
    )
    b = compute_operation_id_update(
        plan_id="plan_001",
        step_id="s2",
        action="update",
        entity_type="shopping_list_item",
        entity_id="sh_abc",
    )
    assert a == b


def test_create_and_update_op_ids_differ():
    """create_id(logical_key=X) and update_id(entity_id=X) must not
    collide — different semantics. The function-name differentiation
    is enforced by the action being baked into the hash."""
    create_id = compute_operation_id_create(
        plan_id="p",
        step_id="s",
        action="add",
        entity_type="task",
        logical_key="X",
    )
    update_id = compute_operation_id_update(
        plan_id="p",
        step_id="s",
        action="update",
        entity_type="task",
        entity_id="X",
    )
    assert create_id != update_id


# ---------------------------------------------------------------------------
# compute_normalized_title_hash
# ---------------------------------------------------------------------------


def test_normalized_title_hash_collapses_inflections():
    """Same lemmatized form → same hash."""
    a = compute_normalized_title_hash("молоко")
    b = compute_normalized_title_hash("молока")
    assert a == b


def test_normalized_title_hash_is_deterministic_hex():
    """Fixed-length hex string — usable in TEXT/VARCHAR(64) columns."""
    h = compute_normalized_title_hash("молоко")
    assert len(h) == 64  # SHA-256 → 64 hex chars
    assert all(c in "0123456789abcdef" for c in h)


def test_normalized_title_hash_empty_input():
    """Empty input → empty string (caller treats as 'no dedup possible')."""
    assert compute_normalized_title_hash("") == ""
    assert compute_normalized_title_hash("   ") == ""


def test_normalized_title_hash_distinct_items_differ():
    """Different lemmas → different hashes."""
    a = compute_normalized_title_hash("молоко")
    b = compute_normalized_title_hash("хлеб")
    assert a != b


def test_normalized_title_hash_keyed_by_env_key(monkeypatch):
    """Codex R1 MAJOR #4 — different SREDA_ENCRYPTION_KEY values
    produce different hashes for the same input, confirming HMAC
    is in effect (not plain SHA-256)."""
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", "key_alpha_" + "0" * 50)
    a = compute_normalized_title_hash("молоко")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", "key_beta_" + "f" * 50)
    b = compute_normalized_title_hash("молоко")
    assert a != b, (
        "HMAC keyed by SREDA_ENCRYPTION_KEY should produce distinct "
        "hashes for the same input under different keys — got identical, "
        "implying plain SHA-256 fallback is in play."
    )


def test_normalized_title_hash_fallback_when_key_empty(monkeypatch):
    """When SREDA_ENCRYPTION_KEY is empty (dev / test default), the
    function falls back to plain SHA-256 — but over the *scoped*
    payload (NORMALIZATION_VERSION + entity_type + tenant + user +
    normalized), not the bare normalized title. Codex R2 MAJOR #3."""
    import hashlib
    from sreda.services.text_normalization import NORMALIZATION_VERSION
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", "")
    h = compute_normalized_title_hash("молоко")
    sep = "\x1f"
    expected_payload = sep.join([
        f"v{NORMALIZATION_VERSION}",
        "",  # entity_type default
        "",  # tenant_id default
        "",  # user_id default
        "молоко",
    ])
    expected = hashlib.sha256(expected_payload.encode("utf-8")).hexdigest()
    assert h == expected


def test_normalized_title_hash_entity_type_separates_domains():
    """Codex R2 MAJOR #3 — same title across different entity types
    must hash differently to prevent cross-table equality leak."""
    a = compute_normalized_title_hash("молоко", entity_type="shopping_list_item")
    b = compute_normalized_title_hash("молоко", entity_type="task")
    assert a != b


def test_normalized_title_hash_tenant_user_scope_separates():
    """Codex R2 MAJOR #3 — different tenant or user → different hash
    even for the same title + entity_type."""
    base = dict(entity_type="shopping_list_item", title="молоко")
    a = compute_normalized_title_hash(**base, tenant_id="t1", user_id="u1")
    b = compute_normalized_title_hash(**base, tenant_id="t2", user_id="u1")
    c = compute_normalized_title_hash(**base, tenant_id="t1", user_id="u2")
    assert a != b
    assert a != c
    assert b != c
