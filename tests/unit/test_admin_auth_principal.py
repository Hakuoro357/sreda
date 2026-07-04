"""#305 — ``require_admin_token`` returns an ``AdminPrincipal`` (Phase 2 auth).

Named tests for the acceptance checklist:

- Valid ``admin_tg_session`` cookie whose tg_id is in the allowlist → telegram
  principal, ``actor_id == "admin_tg:<id>"`` (checklist п.2/п.5).
- tg_id NOT in the allowlist (revocation via env) → 401 on the NEXT request even
  though the session row is live (checklist п.4).
- legacy fallback token still authenticates → token principal (checklist п.9).
- 403 only when NEITHER token NOR allowlist is configured (admin disabled).
- No template/redirect emits a raw token — actor_id is ``admin_token`` /
  ``admin_tg:<id>``, never the secret (checklist п.6).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sreda.admin import auth as auth_mod
from sreda.admin.auth import AdminPrincipal, require_admin_token
from sreda.db.base import Base
import sreda.db.models.admin_auth  # noqa: F401
from sreda.services import admin_login as al


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _settings(*, admin_token=None, admin_tg_ids=()):
    return SimpleNamespace(admin_token=admin_token, admin_tg_ids=frozenset(admin_tg_ids))


def _patch(monkeypatch, session_factory, settings):
    monkeypatch.setattr(auth_mod, "get_settings", lambda: settings)
    # _resolve_tg_principal + resolve_session read get_session_factory() lazily.
    monkeypatch.setattr(
        "sreda.db.session.get_session_factory", lambda: session_factory
    )


def _request(path="/admin/"):
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )


def test_actor_id_shapes():
    assert AdminPrincipal("telegram", tg_id="42").actor_id == "admin_tg:42"
    assert AdminPrincipal("token").actor_id == "admin_token"


def test_valid_tg_session_in_allowlist_returns_telegram_principal(
    monkeypatch, session_factory
):
    s = session_factory()
    raw = al.mint_session(s, "42")
    s.close()

    _patch(monkeypatch, session_factory, _settings(admin_tg_ids=["42"]))
    principal = require_admin_token(
        _request(), header_token=None, query_token=None,
        tg_cookie=raw, cookie_token=None,
    )
    assert principal.auth_method == "telegram"
    assert principal.actor_id == "admin_tg:42"
    # No raw session id leaks into the principal.
    assert raw not in principal.actor_id


def test_tg_id_not_in_allowlist_401_next_request(monkeypatch, session_factory):
    """Отзыв: сессия жива в БД, но tg_id убран из allowlist → 401."""
    s = session_factory()
    raw = al.mint_session(s, "42")
    s.close()

    # allowlist no longer contains 42 (env-based revocation), but token IS set
    # so admin isn't "disabled" → must be 401 (not 403).
    _patch(monkeypatch, session_factory, _settings(admin_token="tok", admin_tg_ids=["999"]))
    with pytest.raises(HTTPException) as exc:
        require_admin_token(
            _request(), header_token=None, query_token=None,
            tg_cookie=raw, cookie_token=None,
        )
    assert exc.value.status_code == 401


def test_legacy_token_fallback_authenticates(monkeypatch, session_factory):
    _patch(monkeypatch, session_factory, _settings(admin_token="secret-tok"))
    principal = require_admin_token(
        _request(), header_token="secret-tok", query_token=None,
        tg_cookie=None, cookie_token=None,
    )
    assert principal.auth_method == "token"
    assert principal.actor_id == "admin_token"


def test_403_only_when_neither_token_nor_allowlist(monkeypatch, session_factory):
    _patch(monkeypatch, session_factory, _settings(admin_token=None, admin_tg_ids=[]))
    with pytest.raises(HTTPException) as exc:
        require_admin_token(
            _request(), header_token=None, query_token=None,
            tg_cookie=None, cookie_token=None,
        )
    assert exc.value.status_code == 403


def test_wrong_token_401_when_configured(monkeypatch, session_factory):
    _patch(monkeypatch, session_factory, _settings(admin_token="right"))
    with pytest.raises(HTTPException) as exc:
        require_admin_token(
            _request(), header_token="wrong", query_token=None,
            tg_cookie=None, cookie_token=None,
        )
    assert exc.value.status_code == 401
