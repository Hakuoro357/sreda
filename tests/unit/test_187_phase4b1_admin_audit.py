"""#187 Phase 4b-1 — admin trigger (soft-delete / restore) + audit.

RED-first (TDD) tests for the acceptance-checklist items this phase covers:

- **A15 (admin trigger, protected)** — soft-delete / restore are reachable
  ONLY through the admin surface, with the SAME protection as every other
  admin action (``require_admin_token``):
    * ``POST /admin/tenant/{id}/soft-delete`` → tenant marked deleted
      (``is_tenant_active`` → False);
    * ``POST /admin/tenant/{id}/restore`` → tenant active again
      (``is_tenant_active`` → True);
    * a request WITHOUT a valid admin token → rejected (401/403), like the
      existing suspend/unsuspend actions — NOT a public endpoint.

- **A11 (audit)** — every admin lifecycle action writes one ``audit_log``
  row with the canonical action key and admin actor/source:
    * soft-delete → ``admin.tenant.soft_delete`` (actor_type=admin,
      actor_id=hash(token), metadata source=admin + previous deleted_at);
    * restore → ``admin.tenant.restore``.
  The audit row is written UNDER the same transaction/lock as the mutation
  (it lives inside ``soft_delete_tenant`` / ``restore_tenant`` so self-delete
  in Phase 4b-2 reuses it) — so a committed flag flip always has its audit row.

- **anti-over-reach** — a non-admin path cannot trigger either action; a
  repeated soft-delete is idempotent (no second audit row, no-op — Phase 2a
  already covers the drain idempotency).

Field/route names below were confirmed against the live code:
- protection mirror: ``admin_tenant_suspend`` at admin/routes.py:1060
  (``token: str = Depends(require_admin_token)``, 303 redirect);
- audit API: ``audit_event(session, *, actor_type, actor_id, action,
  resource_type, resource_id, metadata, commit)`` at services/audit.py:44;
  ``hash_admin_token`` at services/audit.py:106;
- canonical action keys per db/models/audit.py:11 docstring + plan line 77
  (``admin.tenant.soft_delete`` / ``admin.tenant.restore``).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sreda.db.base import Base
from sreda.db.models.audit import AuditLog
from sreda.db.models.core import Tenant
from sreda.services.tenant_lifecycle import (
    is_tenant_active,
    restore_tenant,
    soft_delete_tenant,
)

from tests.unit.conftest import seed_telegram_user

_ADMIN_TOKEN = "test-admin-tok-123456"


def _bootstrap_csrf(client) -> str:
    """#305: obtain a CSRF token bound to the SAME admin_session cookie the POST
    will carry. Admin POSTs now go through ``require_csrf`` (fail-closed) whose
    token binds to the session cookie. A first header-token GET (over https) sets
    ``admin_session`` in the client jar; from then on the token binds to that
    cookie value, matching what the server computes for the follow-up POST."""
    from types import SimpleNamespace

    from sreda.admin.csrf import csrf_token

    # Header-auth GET → middleware sets the admin_session cookie in the response.
    client.get("/admin/users", headers={"X-Admin-Token": _ADMIN_TOKEN})
    sess = client.cookies.get("admin_session")
    cookies = {"admin_session": sess} if sess else {}
    return csrf_token(SimpleNamespace(cookies=cookies, state=SimpleNamespace()))


# ---------------------------------------------------------------------------
# In-memory app harness — shared engine bound to the /admin route session.
# ---------------------------------------------------------------------------


def _make_engine():
    import sreda.db.models  # noqa: F401 — register core tables
    import sreda.db.models.audit  # noqa: F401

    eng = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def admin_client(monkeypatch):
    """TestClient with admin token set + /admin route session bound to a
    shared in-memory engine, so the test can seed/inspect the same DB."""
    from sreda.admin.routes import _get_session
    from sreda.config.settings import get_settings
    from sreda.main import app

    monkeypatch.setenv("SREDA_ADMIN_TOKEN", _ADMIN_TOKEN)
    get_settings.cache_clear()

    eng = _make_engine()
    SessionLocal = sessionmaker(bind=eng)

    def _override_session():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[_get_session] = _override_session
    client = TestClient(app, base_url="https://testserver")
    try:
        yield client, SessionLocal
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        eng.dispose()


def _seed_tenant(session: Session, tid: str, chat_id: str, user_id: str) -> None:
    seed_telegram_user(
        session,
        tenant_id=tid,
        chat_id=chat_id,
        user_id=user_id,
        workspace_id=f"ws_{tid}",
        profile=False,
    )
    session.commit()


# ---------------------------------------------------------------------------
# A15 — admin trigger flips the flag, protected like other admin actions
# ---------------------------------------------------------------------------


def test_admin_soft_delete_marks_tenant_deleted(admin_client) -> None:
    """A15: authenticated admin POST → tenant marked deleted (inactive)."""
    client, SessionLocal = admin_client
    tid = "tenant_admin_del"
    with SessionLocal() as s:
        _seed_tenant(s, tid, "555", "u_admin_del")
        assert is_tenant_active(s, tid) is True

    r = client.post(
        f"/admin/tenant/{tid}/soft-delete",
        headers={"X-Admin-Token": _ADMIN_TOKEN},
        data={"csrf": _bootstrap_csrf(client)},
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)

    with SessionLocal() as s:
        assert is_tenant_active(s, tid) is False


def test_admin_restore_marks_tenant_active(admin_client) -> None:
    """A15: authenticated admin restore POST → tenant active again."""
    client, SessionLocal = admin_client
    tid = "tenant_admin_restore"
    with SessionLocal() as s:
        _seed_tenant(s, tid, "556", "u_admin_restore")
        soft_delete_tenant(s, tid)
        assert is_tenant_active(s, tid) is False

    r = client.post(
        f"/admin/tenant/{tid}/restore",
        headers={"X-Admin-Token": _ADMIN_TOKEN},
        data={"csrf": _bootstrap_csrf(client)},
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)

    with SessionLocal() as s:
        assert is_tenant_active(s, tid) is True


def test_admin_soft_delete_rejected_without_token(admin_client) -> None:
    """A15 / anti-over-reach: no admin token → rejected, tenant untouched."""
    client, SessionLocal = admin_client
    tid = "tenant_noauth"
    with SessionLocal() as s:
        _seed_tenant(s, tid, "557", "u_noauth")

    r = client.post(
        f"/admin/tenant/{tid}/soft-delete",
        follow_redirects=False,
    )
    assert r.status_code in (401, 403)

    with SessionLocal() as s:
        # Not deleted — the unauthenticated request must be a no-op.
        assert is_tenant_active(s, tid) is True


def test_admin_restore_rejected_without_token(admin_client) -> None:
    """A15 / anti-over-reach: no admin token → restore rejected."""
    client, SessionLocal = admin_client
    tid = "tenant_noauth_restore"
    with SessionLocal() as s:
        _seed_tenant(s, tid, "558", "u_noauth_restore")
        soft_delete_tenant(s, tid)

    r = client.post(
        f"/admin/tenant/{tid}/restore",
        follow_redirects=False,
    )
    assert r.status_code in (401, 403)

    with SessionLocal() as s:
        # Still deleted — unauthenticated restore must be a no-op.
        assert is_tenant_active(s, tid) is False


# ---------------------------------------------------------------------------
# A11 — audit log written for each admin lifecycle action
# ---------------------------------------------------------------------------


def test_admin_soft_delete_writes_audit(admin_client) -> None:
    """A11: admin soft-delete → one `admin.tenant.soft_delete` audit row."""
    client, SessionLocal = admin_client
    tid = "tenant_audit_del"
    with SessionLocal() as s:
        _seed_tenant(s, tid, "559", "u_audit_del")

    client.post(
        f"/admin/tenant/{tid}/soft-delete",
        headers={"X-Admin-Token": _ADMIN_TOKEN},
        data={"csrf": _bootstrap_csrf(client)},
        follow_redirects=False,
    )

    with SessionLocal() as s:
        rows = (
            s.query(AuditLog)
            .filter(
                AuditLog.action == "admin.tenant.soft_delete",
                AuditLog.resource_id == tid,
            )
            .all()
        )
        assert len(rows) == 1, "exactly one soft_delete audit row expected"
        row = rows[0]
        assert row.actor_type == "admin"
        # #305: token-auth actor is now the stable ``admin_token`` principal id,
        # not the hashed raw token (audit uses principal.actor_id).
        assert row.actor_id == "admin_token"
        assert row.resource_type == "tenant"
        md = json.loads(row.metadata_json)
        assert md.get("source") == "admin"


def test_admin_restore_writes_audit(admin_client) -> None:
    """A11: admin restore → one `admin.tenant.restore` audit row."""
    client, SessionLocal = admin_client
    tid = "tenant_audit_restore"
    with SessionLocal() as s:
        _seed_tenant(s, tid, "560", "u_audit_restore")
        soft_delete_tenant(s, tid)

    client.post(
        f"/admin/tenant/{tid}/restore",
        headers={"X-Admin-Token": _ADMIN_TOKEN},
        data={"csrf": _bootstrap_csrf(client)},
        follow_redirects=False,
    )

    with SessionLocal() as s:
        rows = (
            s.query(AuditLog)
            .filter(
                AuditLog.action == "admin.tenant.restore",
                AuditLog.resource_id == tid,
            )
            .all()
        )
        assert len(rows) == 1, "exactly one restore audit row expected"
        row = rows[0]
        assert row.actor_type == "admin"
        # #305: token-auth actor is now the stable ``admin_token`` principal id.
        assert row.actor_id == "admin_token"
        assert row.resource_type == "tenant"
        md = json.loads(row.metadata_json)
        assert md.get("source") == "admin"


# ---------------------------------------------------------------------------
# Audit lives inside the lifecycle fns (atomic w/ flag) — unit-level proof,
# so self-delete (Phase 4b-2) reuses the same path.
# ---------------------------------------------------------------------------


def test_soft_delete_audit_is_atomic_with_flag(db_session: Session) -> None:
    """A11: audit row committed together with the deleted_at flag (one tx)."""
    tid = "tenant_atomic_del"
    _seed_tenant(db_session, tid, "561", "u_atomic_del")

    changed = soft_delete_tenant(
        db_session,
        tid,
        actor_type="admin",
        actor_id="hash_abc",
        source="admin",
    )
    assert changed is True
    assert is_tenant_active(db_session, tid) is False

    rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "admin.tenant.soft_delete")
        .all()
    )
    assert len(rows) == 1
    md = json.loads(rows[0].metadata_json)
    assert md.get("source") == "admin"
    # F2: the tenant was ACTIVE before delete → previous_deleted_at is None
    # (the trace anchor recorded by soft_delete — plan A11 metadata).
    assert md.get("previous_deleted_at") is None


def test_soft_delete_idempotent_no_second_audit(db_session: Session) -> None:
    """anti-over-reach: a repeated soft-delete writes NO second audit row."""
    tid = "tenant_idem_audit"
    _seed_tenant(db_session, tid, "562", "u_idem_audit")

    first = soft_delete_tenant(
        db_session, tid, actor_type="admin", actor_id="h", source="admin"
    )
    second = soft_delete_tenant(
        db_session, tid, actor_type="admin", actor_id="h", source="admin"
    )
    assert first is True
    assert second is False  # already deleted → no-op

    rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "admin.tenant.soft_delete")
        .all()
    )
    assert len(rows) == 1, "idempotent re-delete must not write a 2nd audit row"


def test_restore_audit_is_atomic_with_flag(db_session: Session) -> None:
    """A11: restore writes an `admin.tenant.restore` audit row atomically."""
    tid = "tenant_atomic_restore"
    _seed_tenant(db_session, tid, "563", "u_atomic_restore")
    soft_delete_tenant(db_session, tid)

    # F2: capture the deleted_at the restore must echo into the audit metadata.
    # restore_tenant normalizes deleted_at to aware-UTC before recording it
    # (tenant_lifecycle.py: deleted_at.replace(tzinfo=utc) on SQLite-naive),
    # so the expected ISO string carries the +00:00 offset — mirror that here.
    from datetime import timezone

    prev_deleted_at = db_session.get(Tenant, tid).deleted_at
    assert prev_deleted_at is not None
    if prev_deleted_at.tzinfo is None:
        prev_deleted_at = prev_deleted_at.replace(tzinfo=timezone.utc)

    changed = restore_tenant(
        db_session, tid, actor_type="admin", actor_id="hash_def", source="admin"
    )
    assert changed is True
    assert is_tenant_active(db_session, tid) is True

    rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "admin.tenant.restore")
        .all()
    )
    assert len(rows) == 1
    md = json.loads(rows[0].metadata_json)
    assert md.get("source") == "admin"
    # F2: restore audit metadata carries previous_deleted_at = ISO of the
    # deleted_at value that was in force before restore (deletion-window trace).
    assert md.get("previous_deleted_at") == prev_deleted_at.isoformat()


# ---------------------------------------------------------------------------
# F2 — backward-compatibility: legacy (no-audit) calls write ZERO audit rows.
# Phases 2a/2b/3 call soft_delete_tenant / restore_tenant WITHOUT audit params;
# that system path must keep writing no audit row (and must not raise).
# ---------------------------------------------------------------------------


def test_soft_delete_without_audit_params_writes_no_audit(
    db_session: Session,
) -> None:
    """Backward-compat: soft_delete_tenant(session, tid) → zero audit rows."""
    tid = "tenant_legacy_del"
    _seed_tenant(db_session, tid, "564", "u_legacy_del")

    changed = soft_delete_tenant(db_session, tid)
    assert changed is True
    assert is_tenant_active(db_session, tid) is False

    rows = db_session.query(AuditLog).filter(AuditLog.resource_id == tid).all()
    assert rows == [], "legacy (no-actor) soft-delete must write no audit row"


def test_restore_without_audit_params_writes_no_audit(
    db_session: Session,
) -> None:
    """Backward-compat: restore_tenant(session, tid) → zero audit rows."""
    tid = "tenant_legacy_restore"
    _seed_tenant(db_session, tid, "565", "u_legacy_restore")
    soft_delete_tenant(db_session, tid)

    changed = restore_tenant(db_session, tid)
    assert changed is True
    assert is_tenant_active(db_session, tid) is True

    rows = db_session.query(AuditLog).filter(AuditLog.resource_id == tid).all()
    assert rows == [], "legacy (no-actor) restore must write no audit row"


# ---------------------------------------------------------------------------
# F1 (R1 MAJOR) — strict audit: when an actor is supplied but the audit write
# fails (audit_event returns None), the lifecycle fn RAISES before commit and
# the deleted_at flag is NOT set (atomicity: committed flag ⟺ audit row).
#
# These tests use a DEDICATED standalone engine/session (NOT the shared
# ``db_session`` fixture): the fixture wraps every test in an outer
# ``conn.begin()`` and the lifecycle fns call ``session.commit()`` inside it,
# which makes a post-raise ``session.rollback()`` rewind unpredictably. A plain
# committing session reproduces the real admin-endpoint transaction model
# (commit-or-rollback) faithfully — after the raise we rollback() and assert
# the flag never became durable.
# ---------------------------------------------------------------------------


@pytest.fixture
def standalone_session():
    eng = _make_engine()
    SessionLocal = sessionmaker(bind=eng)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
        eng.dispose()


def test_soft_delete_raises_and_leaves_flag_unset_on_audit_failure(
    standalone_session: Session,
) -> None:
    """F1: actor given + audit returns None → RuntimeError, flag NOT set.

    Two independent triggers for audit_event → None are exercised:
      (a) invalid actor_type (audit.py:71-75 — skip, return None);
      (b) a mocked audit_event that returns None (flush-failure surrogate).
    Both must abort the mutation: no durable deleted_at, no audit row, raise.
    """
    s = standalone_session

    # (a) invalid actor_type — audit_event returns None without writing.
    tid_a = "tenant_strict_invalid_actor"
    _seed_tenant(s, tid_a, "566", "u_strict_invalid")
    with pytest.raises(RuntimeError, match="audit"):
        soft_delete_tenant(
            s, tid_a, actor_type="not-a-valid-actor", actor_id="h", source="admin"
        )
    # Codex R2 MAJOR: вызывающий КОММИТИТ после catch (не rollback) — флаг ВСЁ
    # РАВНО не должен стать durable, потому что функция откатила pending САМА.
    # Без внутреннего session.rollback() этот commit зафиксировал бы флаг без аудита.
    s.commit()
    assert is_tenant_active(s, tid_a) is True, "flag must stay unset (a) даже при commit вызывающего"
    assert s.query(AuditLog).filter(AuditLog.resource_id == tid_a).all() == []

    # (b) audit_event mocked to return None (e.g. caught flush error surrogate).
    # _write_lifecycle_audit does a function-local ``from ... import audit_event``,
    # so patching the module attribute is picked up at call time.
    tid_b = "tenant_strict_audit_none"
    _seed_tenant(s, tid_b, "567", "u_strict_none")
    import sreda.services.audit as audit_mod

    orig = audit_mod.audit_event
    try:
        audit_mod.audit_event = lambda *a, **k: None
        with pytest.raises(RuntimeError, match="audit"):
            soft_delete_tenant(
                s, tid_b, actor_type="admin", actor_id="h", source="admin"
            )
    finally:
        audit_mod.audit_event = orig
    s.commit()  # вызывающий коммитит после catch — флаг не durable (функция откатила)
    assert is_tenant_active(s, tid_b) is True, "flag must stay unset (b) даже при commit вызывающего"


def test_restore_raises_and_keeps_flag_on_audit_failure(
    standalone_session: Session,
) -> None:
    """F1 (symmetric): restore with actor + audit None → raise, stays deleted."""
    s = standalone_session
    tid = "tenant_strict_restore"
    _seed_tenant(s, tid, "568", "u_strict_restore")
    soft_delete_tenant(s, tid)  # legacy delete (no actor) commits the flag
    assert is_tenant_active(s, tid) is False

    with pytest.raises(RuntimeError, match="audit"):
        restore_tenant(
            s, tid, actor_type="not-a-valid-actor", actor_id="h", source="admin"
        )
    # Codex R3 MINOR: вызывающий КОММИТИТ после catch (симметрично soft-delete) —
    # снятие флага НЕ должно стать durable, т.к. restore_tenant откатил pending САМ.
    s.commit()
    # Restore aborted → tenant must remain deleted (flag NOT cleared, durable).
    assert is_tenant_active(s, tid) is False, "restore must not flip flag даже при commit вызывающего"
    assert (
        s.query(AuditLog).filter(AuditLog.action == "admin.tenant.restore").all()
        == []
    )
