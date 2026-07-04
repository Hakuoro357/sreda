"""#305 — admin login bot branches via ``handle_telegram_update`` (Phase 4).

The two EARLY branches (before the soft-delete gate + onboarding) must:

- ``/start adm_<challenge_id>`` from an ALLOWLISTED tg_id → send the confirm
  button, record message_id, and NOT fall through to welcome/chat/persist
  (checklist п.8). A DUPLICATE update sends NO second button (send-claim
  at-most-once, checklist п.8).
- a NON-admin ``/start adm_`` does NOT move the challenge (stays pending, no
  bot attached) (checklist п.3/п.8).
- ``adm_confirm:`` callback from a non-admin does NOT confirm (checklist п.3).

Harness: on-disk SQLite wired through the prod caches (like
``test_187_phase4a_perimeter.fresh_db``); ``telegram_client_for`` patched to a
fake so no network call happens.
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sreda.db.base import Base
import sreda.db.models  # noqa: F401
import sreda.db.models.admin_auth  # noqa: F401


@pytest.fixture()
def fresh_db(monkeypatch, tmp_path: Path):
    from sreda.config.settings import get_settings
    from sreda.db.session import get_engine, get_session_factory

    db_path = tmp_path / "adm.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "tg-token")
    monkeypatch.setenv("SREDA_TG_ACCOUNT_SALT", "test-salt")
    monkeypatch.setenv("SREDA_ADMIN_TG_IDS", "42")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


class _FakeClient:
    """Records sends and returns a Telegram-shaped result with a message_id."""

    def __init__(self, token: str) -> None:
        self.token = token

    async def send_message(self, **kwargs):
        _SENDS.append(kwargs)
        return {"result": {"message_id": 555}}

    async def answer_callback_query(self, *a, **k):
        _ANSWERS.append((a, k))
        return {"ok": True}

    async def delete_message(self, *a, **k):
        _DELETES.append((a, k))
        return {"ok": True}


_SENDS: list = []
_ANSWERS: list = []
_DELETES: list = []


@pytest.fixture(autouse=True)
def _reset_recorders():
    _SENDS.clear()
    _ANSWERS.clear()
    _DELETES.clear()
    yield


def _patch_client(monkeypatch):
    from sreda.services import telegram_inbound as ti
    monkeypatch.setattr(ti, "telegram_client_for", lambda bot_key, reg: _FakeClient("t"))


def _seed_challenge(client_ip="1.2.3.4"):
    from sreda.db.session import get_session_factory
    from sreda.services import admin_login as al

    s = get_session_factory()()
    try:
        return al.start_challenge(s, client_ip)
    finally:
        s.close()


def _start_payload(challenge_id: str, from_id: int):
    return {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "chat": {"id": from_id},
            "from": {"id": from_id},
            "text": f"/start adm_{challenge_id}",
        },
    }


def _confirm_payload(challenge_id: str, from_id: int):
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cbq1",
            "from": {"id": from_id},
            "message": {"message_id": 555, "chat": {"id": from_id}},
            "data": f"adm_confirm:{challenge_id}",
        },
    }


@pytest.mark.asyncio
async def test_start_allowlisted_sends_button_no_welcome(fresh_db, monkeypatch):
    from sreda.db.session import get_session_factory
    from sreda.services import admin_login as al
    from sreda.services.telegram_inbound import handle_telegram_update

    _patch_client(monkeypatch)
    r = _seed_challenge()

    result = await handle_telegram_update(_start_payload(r.challenge_id, 42), bot_key="sreda")
    assert result is None  # early-return branch (no inbound persisted)

    # Exactly one button sent, carrying the human_code + confirm callback.
    assert len(_SENDS) == 1
    sent = _SENDS[0]
    assert r.human_code in sent["text"]
    kb = sent["reply_markup"]["inline_keyboard"][0][0]
    assert kb["callback_data"] == f"adm_confirm:{r.challenge_id}"

    # No welcome/chat: no inbound_messages row was created.
    from sreda.db.models.core import InboundMessage
    s = get_session_factory()()
    try:
        assert s.query(InboundMessage).count() == 0
        # message_id recorded on the challenge.
        assert al.get_status(s, r.challenge_id, r.browser_bind_raw) == "pending"
    finally:
        s.close()


@pytest.mark.asyncio
async def test_duplicate_start_no_second_button(fresh_db, monkeypatch):
    from sreda.services.telegram_inbound import handle_telegram_update

    _patch_client(monkeypatch)
    r = _seed_challenge()

    await handle_telegram_update(_start_payload(r.challenge_id, 42), bot_key="sreda")
    await handle_telegram_update(_start_payload(r.challenge_id, 42), bot_key="sreda")
    # send-claim at-most-once → only ONE button send across two updates.
    assert len(_SENDS) == 1


@pytest.mark.asyncio
async def test_non_admin_start_does_not_move_challenge(fresh_db, monkeypatch):
    from sreda.db.session import get_session_factory
    from sreda.db.models.admin_auth import AdminLoginChallenge
    from sreda.services.telegram_inbound import handle_telegram_update

    _patch_client(monkeypatch)
    r = _seed_challenge()

    # tg_id 999 is NOT in the allowlist ("42").
    result = await handle_telegram_update(_start_payload(r.challenge_id, 999), bot_key="sreda")
    assert result is None  # handled (neutral), NOT fallthrough

    s = get_session_factory()()
    try:
        row = (
            s.query(AdminLoginChallenge)
            .filter(AdminLoginChallenge.challenge_id == r.challenge_id)
            .one()
        )
        # Challenge untouched: no bot attached, still pending, no send-claim.
        assert row.status == "pending"
        assert row.tg_id is None
        assert row.button_send_claimed_at is None
    finally:
        s.close()
    # No confirm button was sent to a non-admin.
    assert all(str(s.get("text", "")).find("Подтвердить") == -1 for s in _SENDS)


@pytest.mark.asyncio
async def test_non_admin_confirm_does_not_confirm(fresh_db, monkeypatch):
    from sreda.db.session import get_session_factory
    from sreda.services import admin_login as al
    from sreda.services.telegram_inbound import handle_telegram_update

    _patch_client(monkeypatch)
    r = _seed_challenge()
    # An admin started + a button exists.
    s = get_session_factory()()
    al.attach_bot(s, r.challenge_id, "42", "sreda", "42")
    s.close()

    # Non-admin taps confirm → must not advance status.
    result = await handle_telegram_update(_confirm_payload(r.challenge_id, 999), bot_key="sreda")
    assert result is None
    s = get_session_factory()()
    try:
        assert al.get_status(s, r.challenge_id, r.browser_bind_raw) == "pending"
    finally:
        s.close()


@pytest.mark.asyncio
async def test_admin_confirm_advances_and_deletes_button(fresh_db, monkeypatch):
    from sreda.db.session import get_session_factory
    from sreda.services import admin_login as al
    from sreda.services.telegram_inbound import handle_telegram_update

    _patch_client(monkeypatch)
    r = _seed_challenge()
    s = get_session_factory()()
    al.attach_bot(s, r.challenge_id, "42", "sreda", "42")
    al.record_confirm_message(s, r.challenge_id, 555)
    s.close()

    result = await handle_telegram_update(_confirm_payload(r.challenge_id, 42), bot_key="sreda")
    assert result is None
    s = get_session_factory()()
    try:
        assert al.get_status(s, r.challenge_id, r.browser_bind_raw) == "confirmed"
    finally:
        s.close()
    # callback answered + best-effort button delete attempted.
    assert len(_ANSWERS) == 1
    assert len(_DELETES) == 1
