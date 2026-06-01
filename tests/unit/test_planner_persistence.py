"""Unit tests for runtime/planner/persistence.py — Sub-A12 Phase E PR-2a #10a.

Covers:
* PlannerPiiWriteMode default (encrypted_only): plaintext columns NULL,
  *_enc columns non-NULL, read_planner_pii returns original value.
* dual_write mode: both plaintext and *_enc written; read_planner_pii
  returns value from *_enc (preferred).
* plaintext_only mode: *_enc NULL, plaintext set; read_planner_pii falls
  back to plaintext.
* JSON column (plan_json) and str column (raw_planner_response) round-
  trips to prove JSONEncryptedString vs EncryptedString handling.
* The *_enc raw storage value looks like a v1:/v2: envelope (ciphertext),
  not the original cleartext.
* mark_received / mark_invalid / mark_valid each route PII writes
  correctly under the default (encrypted_only) mode.
* read_planner_pii rejects unknown field names.

SREDA_ENCRYPTION_KEY is injected automatically by the conftest
``encryption_key`` fixture (auto-use), so no per-test setup is needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from sreda.db.models import (
    AgentRun,
    AgentThread,
    PlannerExecution,
    Tenant,
    Workspace,
)
from sreda.runtime.planner.persistence import (
    _PLANNER_PII_WRITE_MODE,
    _write_pii,
    insert_pending,
    make_execution_id,
    mark_invalid,
    mark_received,
    mark_valid,
    read_planner_pii,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENCRYPTED_PREFIXES = ("v1:", "v2:")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed_minimal_run(session: Session) -> str:
    """Insert Tenant + Workspace + AgentThread + AgentRun; return run_id."""
    tenant_id = f"tenant_{uuid4().hex[:12]}"
    ws_id = f"ws_{uuid4().hex[:12]}"
    thread_id = f"thread_{uuid4().hex[:12]}"
    run_id = f"run_{uuid4().hex[:12]}"

    session.add(Tenant(id=tenant_id, name="t"))
    session.add(Workspace(id=ws_id, tenant_id=tenant_id, name="w"))
    session.add(
        AgentThread(
            id=thread_id,
            tenant_id=tenant_id,
            workspace_id=ws_id,
            channel_type="telegram",
            external_chat_id="42",
        )
    )
    session.add(
        AgentRun(
            id=run_id,
            thread_id=thread_id,
            tenant_id=tenant_id,
            workspace_id=ws_id,
            action_type="chat",
        )
    )
    session.flush()
    return run_id


def _make_execution_id() -> str:
    return make_execution_id()


def _insert_pending(session: Session, run_id: str, eid: str) -> None:
    insert_pending(
        session,
        execution_id=eid,
        run_id=run_id,
        tenant_id="tenant_test",
        feature_key="housewife_assistant",
        planner_prompt_version=1,
        planner_provider="mimo-v2.5",
        planner_model="mimo-v2.5-pro",
    )


def _raw_enc_value(session: Session, eid: str, col: str) -> Any:
    """Bypass ORM TypeDecorator: read raw stored text from DB."""
    row = session.execute(
        text(f"SELECT {col} FROM planner_executions WHERE id = :id"),  # noqa: S608
        {"id": eid},
    ).mappings().first()
    assert row is not None, f"execution {eid!r} not found"
    return row[col]


# ---------------------------------------------------------------------------
# Default write mode is encrypted_only
# ---------------------------------------------------------------------------


def test_default_write_mode_is_encrypted_only() -> None:
    """Module-level default must be encrypted_only per plan §5."""
    assert _PLANNER_PII_WRITE_MODE == "encrypted_only"


# ---------------------------------------------------------------------------
# mark_received — encrypted_only
# ---------------------------------------------------------------------------


def test_mark_received_encrypted_only_plaintext_is_null(db_session: Session) -> None:
    """encrypted_only: raw_planner_response column is NULL."""
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)

    mark_received(db_session, execution_id=eid, raw_response="купи молоко", latency_ms=100)
    db_session.expire_all()

    row = db_session.get(PlannerExecution, eid)
    assert row.raw_planner_response is None


def test_mark_received_encrypted_only_enc_is_nonnull(db_session: Session) -> None:
    """encrypted_only: raw_planner_response_enc is populated."""
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)

    mark_received(db_session, execution_id=eid, raw_response="купи молоко", latency_ms=100)
    db_session.expire_all()

    row = db_session.get(PlannerExecution, eid)
    assert row.raw_planner_response_enc is not None


def test_mark_received_encrypted_only_read_planner_pii_returns_value(
    db_session: Session,
) -> None:
    """encrypted_only: read_planner_pii returns the original str value."""
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)

    mark_received(db_session, execution_id=eid, raw_response="купи молоко", latency_ms=100)
    db_session.expire_all()

    row = db_session.get(PlannerExecution, eid)
    assert read_planner_pii(row, "raw_planner_response") == "купи молоко"


def test_mark_received_encrypted_only_raw_storage_is_ciphertext(
    db_session: Session,
) -> None:
    """encrypted_only: raw DB value for *_enc is an envelope (v1:/v2:), not cleartext."""
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)

    mark_received(db_session, execution_id=eid, raw_response="купи молоко", latency_ms=100)
    db_session.flush()

    raw = _raw_enc_value(db_session, eid, "raw_planner_response_enc")
    assert isinstance(raw, str)
    assert raw.startswith(_ENCRYPTED_PREFIXES), (
        f"expected v1:/v2: envelope, got: {raw[:40]!r}"
    )
    assert "купи молоко" not in raw


# ---------------------------------------------------------------------------
# mark_invalid — encrypted_only
# ---------------------------------------------------------------------------


def test_mark_invalid_encrypted_only_plaintext_is_null(db_session: Session) -> None:
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)
    mark_received(db_session, execution_id=eid, raw_response="garbage", latency_ms=10)

    mark_invalid(db_session, execution_id=eid, validation_errors="json_decode_error: bad")
    db_session.expire_all()

    row = db_session.get(PlannerExecution, eid)
    assert row.validation_errors is None


def test_mark_invalid_encrypted_only_read_planner_pii_returns_value(
    db_session: Session,
) -> None:
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)
    mark_received(db_session, execution_id=eid, raw_response="garbage", latency_ms=10)

    mark_invalid(db_session, execution_id=eid, validation_errors="json_decode_error: bad")
    db_session.expire_all()

    row = db_session.get(PlannerExecution, eid)
    assert read_planner_pii(row, "validation_errors") == "json_decode_error: bad"


def test_mark_invalid_encrypted_only_enc_is_ciphertext(db_session: Session) -> None:
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)
    mark_received(db_session, execution_id=eid, raw_response="garbage", latency_ms=10)

    mark_invalid(db_session, execution_id=eid, validation_errors="json_decode_error: bad")
    db_session.flush()

    raw = _raw_enc_value(db_session, eid, "validation_errors_enc")
    assert isinstance(raw, str)
    assert raw.startswith(_ENCRYPTED_PREFIXES)
    assert "json_decode_error" not in raw


# ---------------------------------------------------------------------------
# mark_valid — encrypted_only
# ---------------------------------------------------------------------------

_SAMPLE_PLAN = {
    "schema_version": 1,
    "turn_classification": {"is_new_turn": True, "reason": "test"},
    "clarity": "clear",
    "actions": {"s1": {"tool": "add_shopping_items", "args": {}}},
    "compose": {"kind": "template", "template_id": "x", "template_data": {}},
}
_SAMPLE_EXEC_PLAN = {"layers": [["s1"]], "fail_modes": {}}


def test_mark_valid_encrypted_only_plaintext_columns_null(db_session: Session) -> None:
    """encrypted_only: plan_json, execution_plan_json, turn_classification_reason all NULL."""
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)
    mark_received(db_session, execution_id=eid, raw_response="{}", latency_ms=50)

    mark_valid(
        db_session,
        execution_id=eid,
        plan_json=_SAMPLE_PLAN,
        execution_plan_json=_SAMPLE_EXEC_PLAN,
        is_new_turn=True,
        turn_classification_reason="new topic",
    )
    db_session.expire_all()

    row = db_session.get(PlannerExecution, eid)
    assert row.plan_json is None
    assert row.execution_plan_json is None
    assert row.turn_classification_reason is None


def test_mark_valid_encrypted_only_enc_columns_nonnull(db_session: Session) -> None:
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)
    mark_received(db_session, execution_id=eid, raw_response="{}", latency_ms=50)

    mark_valid(
        db_session,
        execution_id=eid,
        plan_json=_SAMPLE_PLAN,
        execution_plan_json=_SAMPLE_EXEC_PLAN,
        is_new_turn=True,
        turn_classification_reason="new topic",
    )
    db_session.expire_all()

    row = db_session.get(PlannerExecution, eid)
    assert row.plan_json_enc is not None
    assert row.execution_plan_json_enc is not None
    assert row.turn_classification_reason_enc is not None


def test_mark_valid_encrypted_only_read_planner_pii_plan_json(db_session: Session) -> None:
    """read_planner_pii(row, 'plan_json') returns the original dict."""
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)
    mark_received(db_session, execution_id=eid, raw_response="{}", latency_ms=50)

    mark_valid(
        db_session,
        execution_id=eid,
        plan_json=_SAMPLE_PLAN,
        execution_plan_json=_SAMPLE_EXEC_PLAN,
        is_new_turn=True,
        turn_classification_reason="new topic",
    )
    db_session.expire_all()

    row = db_session.get(PlannerExecution, eid)
    assert read_planner_pii(row, "plan_json") == _SAMPLE_PLAN
    assert read_planner_pii(row, "execution_plan_json") == _SAMPLE_EXEC_PLAN
    assert read_planner_pii(row, "turn_classification_reason") == "new topic"


def test_mark_valid_encrypted_only_plan_json_enc_is_ciphertext(db_session: Session) -> None:
    """Raw DB value for plan_json_enc is a v1:/v2: envelope, not JSON text."""
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)
    mark_received(db_session, execution_id=eid, raw_response="{}", latency_ms=50)

    mark_valid(
        db_session,
        execution_id=eid,
        plan_json=_SAMPLE_PLAN,
        execution_plan_json=_SAMPLE_EXEC_PLAN,
        is_new_turn=False,
        turn_classification_reason=None,
    )
    db_session.flush()

    raw = _raw_enc_value(db_session, eid, "plan_json_enc")
    assert isinstance(raw, str)
    assert raw.startswith(_ENCRYPTED_PREFIXES), f"expected envelope, got: {raw[:40]!r}"
    # The cleartext JSON must not appear verbatim in the stored blob.
    assert "schema_version" not in raw


# ---------------------------------------------------------------------------
# dual_write mode — _write_pii direct
# ---------------------------------------------------------------------------


def test_write_pii_dual_write_sets_both_attrs(db_session: Session) -> None:
    """dual_write: both plain_attr and enc_attr are set to the value."""
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)

    db_session.expire_all()
    row = db_session.get(PlannerExecution, eid)

    _write_pii(
        row,
        plain_attr="raw_planner_response",
        enc_attr="raw_planner_response_enc",
        value="dual secret",
        mode="dual_write",
    )
    db_session.flush()
    db_session.expire_all()
    row = db_session.get(PlannerExecution, eid)

    # Plaintext column holds the value.
    assert row.raw_planner_response == "dual secret"
    # Encrypted column also holds the value (TypeDecorator decrypts on read).
    assert row.raw_planner_response_enc == "dual secret"
    # read_planner_pii prefers *_enc (non-None) and returns the value.
    assert read_planner_pii(row, "raw_planner_response") == "dual secret"


def test_write_pii_dual_write_json_dict(db_session: Session) -> None:
    """dual_write: JSON column (plan_json) written to both attrs as dict."""
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)

    db_session.expire_all()
    row = db_session.get(PlannerExecution, eid)
    payload = {"key": "value", "nested": [1, 2, 3]}

    _write_pii(
        row,
        plain_attr="plan_json",
        enc_attr="plan_json_enc",
        value=payload,
        mode="dual_write",
    )
    db_session.flush()
    db_session.expire_all()
    row = db_session.get(PlannerExecution, eid)

    assert row.plan_json == payload
    assert row.plan_json_enc == payload
    assert read_planner_pii(row, "plan_json") == payload


# ---------------------------------------------------------------------------
# plaintext_only mode — _write_pii direct
# ---------------------------------------------------------------------------


def test_write_pii_plaintext_only_enc_is_none(db_session: Session) -> None:
    """plaintext_only: *_enc column stays NULL."""
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)

    db_session.expire_all()
    row = db_session.get(PlannerExecution, eid)

    _write_pii(
        row,
        plain_attr="raw_planner_response",
        enc_attr="raw_planner_response_enc",
        value="legacy text",
        mode="plaintext_only",
    )
    db_session.flush()
    db_session.expire_all()
    row = db_session.get(PlannerExecution, eid)

    assert row.raw_planner_response == "legacy text"
    assert row.raw_planner_response_enc is None
    # read_planner_pii falls back to plaintext when *_enc is None.
    assert read_planner_pii(row, "raw_planner_response") == "legacy text"


def test_write_pii_plaintext_only_json_fallback(db_session: Session) -> None:
    """plaintext_only: JSON column written to plaintext; enc None; read falls back."""
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)

    db_session.expire_all()
    row = db_session.get(PlannerExecution, eid)
    payload = {"hello": "world"}

    _write_pii(
        row,
        plain_attr="plan_json",
        enc_attr="plan_json_enc",
        value=payload,
        mode="plaintext_only",
    )
    db_session.flush()
    db_session.expire_all()
    row = db_session.get(PlannerExecution, eid)

    assert row.plan_json == payload
    assert row.plan_json_enc is None
    assert read_planner_pii(row, "plan_json") == payload


# ---------------------------------------------------------------------------
# read_planner_pii — unknown field
# ---------------------------------------------------------------------------


def test_read_planner_pii_unknown_field_raises(db_session: Session) -> None:
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)
    row = db_session.get(PlannerExecution, eid)

    with pytest.raises(ValueError, match="unknown field"):
        read_planner_pii(row, "nonexistent_column")


# ---------------------------------------------------------------------------
# Non-PII fields are NOT affected by write-mode
# ---------------------------------------------------------------------------


def test_non_pii_fields_unaffected_by_write_mode(db_session: Session) -> None:
    """planner_status, is_new_turn, latency etc. still write to plaintext."""
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)

    mark_received(db_session, execution_id=eid, raw_response="x", latency_ms=77)
    mark_valid(
        db_session,
        execution_id=eid,
        plan_json=_SAMPLE_PLAN,
        execution_plan_json=_SAMPLE_EXEC_PLAN,
        is_new_turn=True,
        turn_classification_reason="test",
    )
    db_session.expire_all()
    row = db_session.get(PlannerExecution, eid)

    assert row.planner_status == "valid"
    assert row.planner_latency_ms == 77
    assert row.is_new_turn is True


# ---------------------------------------------------------------------------
# Codex A/B #10a R1 fixes: call-time mode resolution, stale-mirror clear,
# fail-closed on unknown mode.
# ---------------------------------------------------------------------------


def test_mark_received_respects_runtime_mode_toggle(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mark_* must read _PLANNER_PII_WRITE_MODE at CALL time, so a PR-2b
    deployment toggle actually changes behaviour (not frozen at def-time)."""
    import sreda.runtime.planner.persistence as _p

    monkeypatch.setattr(_p, "_PLANNER_PII_WRITE_MODE", "dual_write")
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)

    mark_received(db_session, execution_id=eid, raw_response="toggled", latency_ms=5)
    db_session.expire_all()

    row = db_session.get(PlannerExecution, eid)
    # dual_write → BOTH set (would be plaintext NULL under the encrypted_only default).
    assert row.raw_planner_response == "toggled"
    assert row.raw_planner_response_enc == "toggled"


def test_write_pii_plaintext_only_clears_stale_enc(db_session: Session) -> None:
    """A plaintext_only write AFTER an encrypted write must NULL the stale
    *_enc mirror, else read_planner_pii (prefers *_enc) returns the OLD value."""
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)
    db_session.expire_all()
    row = db_session.get(PlannerExecution, eid)

    _write_pii(
        row, plain_attr="raw_planner_response",
        enc_attr="raw_planner_response_enc", value="OLD-A", mode="encrypted_only",
    )
    db_session.flush()
    _write_pii(
        row, plain_attr="raw_planner_response",
        enc_attr="raw_planner_response_enc", value="NEW-B", mode="plaintext_only",
    )
    db_session.flush()
    db_session.expire_all()
    row = db_session.get(PlannerExecution, eid)

    assert row.raw_planner_response == "NEW-B"
    assert row.raw_planner_response_enc is None  # stale mirror cleared
    assert read_planner_pii(row, "raw_planner_response") == "NEW-B"  # not stale OLD-A


def test_write_pii_unknown_mode_raises_fail_closed(db_session: Session) -> None:
    """An unknown mode must fail closed (NOT silently dual_write and leak the
    plaintext column) — e.g. a hyphen typo 'encrypted-only'."""
    run_id = _seed_minimal_run(db_session)
    eid = _make_execution_id()
    _insert_pending(db_session, run_id, eid)
    row = db_session.get(PlannerExecution, eid)

    with pytest.raises(ValueError, match="unknown PlannerPiiWriteMode"):
        _write_pii(
            row, plain_attr="raw_planner_response",
            enc_attr="raw_planner_response_enc", value="x",
            mode="encrypted-only",  # type: ignore[arg-type]  # hyphen typo
        )
