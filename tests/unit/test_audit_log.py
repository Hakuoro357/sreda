"""Tests for AuditLog model + audit_event() service.

152-ФЗ Часть 2: проверяем что важные действия (admin / user) корректно
сохраняются и индексируются."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.audit import AuditLog
from sreda.services.audit import audit_event, hash_admin_token


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def test_audit_event_creates_row(session):
    row = audit_event(
        session,
        actor_type="admin",
        actor_id="admin_hash_xyz",
        action="admin.tenant.approve",
        resource_type="tenant",
        resource_id="tenant_123",
        metadata={"reason": "manual"},
    )
    assert row is not None
    assert row.id.startswith("audit_")
    assert row.actor_type == "admin"
    assert row.actor_id == "admin_hash_xyz"
    assert row.action == "admin.tenant.approve"
    assert row.resource_type == "tenant"
    assert row.resource_id == "tenant_123"
    assert json.loads(row.metadata_json) == {"reason": "manual"}
    assert isinstance(row.created_at, datetime)

    # Persisted
    persisted = session.query(AuditLog).filter_by(id=row.id).one()
    assert persisted.action == "admin.tenant.approve"


def test_audit_event_with_no_metadata(session):
    row = audit_event(
        session,
        actor_type="user",
        actor_id="user_x",
        action="user.privacy_consent.given",
    )
    assert row is not None
    assert row.metadata_json == "{}"


def test_audit_event_rejects_invalid_actor_type(session):
    """actor_type должен быть из allowed set; иначе — не пишем."""
    row = audit_event(
        session,
        actor_type="hacker",  # не в _VALID_ACTOR_TYPES
        actor_id="x",
        action="some.action",
    )
    assert row is None
    assert session.query(AuditLog).count() == 0


def test_audit_event_handles_db_error_gracefully(session, monkeypatch):
    """Если БД-операция падает — функция не пробрасывает, возвращает None."""

    def failing_add(_obj):
        raise RuntimeError("db down")

    monkeypatch.setattr(session, "add", failing_add)
    row = audit_event(
        session,
        actor_type="admin",
        actor_id="x",
        action="some.action",
    )
    assert row is None  # graceful


def test_audit_event_commits_when_commit_true(session):
    row = audit_event(
        session,
        actor_type="system",
        actor_id="system",
        action="retention.cleanup",
        commit=True,
    )
    assert row is not None
    # New session sees it (committed)
    Session = sessionmaker(bind=session.get_bind())
    s2 = Session()
    try:
        assert s2.query(AuditLog).count() == 1
    finally:
        s2.close()


def test_audit_event_does_not_commit_when_commit_false(session):
    """commit=False — caller manages transaction."""
    audit_event(
        session,
        actor_type="admin",
        actor_id="x",
        action="action.in.transaction",
        commit=False,
    )
    # Видно в той же сессии (flush был)
    assert session.query(AuditLog).count() == 1

    # Roll back в caller — запись пропадает
    session.rollback()
    assert session.query(AuditLog).count() == 0


def test_hash_admin_token_returns_hex_truncated_to_32():
    h = hash_admin_token("secret-admin-token-1234")
    assert len(h) == 32
    # Hex
    assert all(c in "0123456789abcdef" for c in h)


def test_hash_admin_token_empty_returns_empty():
    assert hash_admin_token("") == ""


def test_hash_admin_token_deterministic():
    assert hash_admin_token("abc") == hash_admin_token("abc")
    assert hash_admin_token("abc") != hash_admin_token("abcd")


def test_indexes_exist(session):
    """Smoke: индексы на action+created, actor, resource — для query
    производительности."""
    engine = session.get_bind()
    indexes = engine.dialect.get_indexes(engine.connect(), "audit_log")
    names = {idx["name"] for idx in indexes}
    assert "ix_audit_log_action_created" in names
    assert "ix_audit_log_actor" in names
    assert "ix_audit_log_resource" in names
