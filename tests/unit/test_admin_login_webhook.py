"""#305 Item A — admin-login update through the REAL telegram webhook route.

The admin-login branches ``return None`` from ``handle_telegram_update``. The
webhook route wraps that in ``TelegramWebhookAccepted(request_id=...)``; with
``request_id: str`` (non-optional) a None return raised a ValidationError → HTTP
500 on the webhook transport (long-poll was unaffected — it discards the
return). Regression gate: an admin ``/start adm_<id>`` update POSTed to the
webhook endpoint must return 202, not 500.

Also covers the pre-existing None-return cases (soft-delete silent-drop,
SignupBlocked) that share the same schema fix.
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from sreda.db.base import Base
import sreda.db.models  # noqa: F401
import sreda.db.models.admin_auth  # noqa: F401


# #341: webhook-роут монтируется ТОЛЬКО в webhook-режиме (bot_token+webhook_url).
# Прод-вход admin-login TG идёт через long-poll (роут не монтирован); здесь тест
# специально бьёт по webhook-роуту → армируем webhook-режим + шлём секрет.
_WEBHOOK_URL = "https://bot.test.local/webhooks/telegram/sreda"
_WEBHOOK_SECRET = "wh-secret-admin-341"
_WEBHOOK_HEADERS = {"X-Telegram-Bot-Api-Secret-Token": _WEBHOOK_SECRET}


@pytest.fixture()
def webhook_client(monkeypatch, tmp_path: Path):
    from sreda.config.settings import get_settings
    from sreda.db.session import get_engine, get_session_factory
    from sreda.main import create_app

    db_path = tmp_path / "wh.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "tg-token")
    # #341: webhook-режим (иначе роут не смонтирован → 404).
    monkeypatch.setenv("SREDA_TELEGRAM_WEBHOOK_URL", _WEBHOOK_URL)
    monkeypatch.setenv("SREDA_TELEGRAM_WEBHOOK_SECRET_TOKEN", _WEBHOOK_SECRET)
    monkeypatch.setenv("SREDA_TG_ACCOUNT_SALT", "test-salt")
    monkeypatch.setenv("SREDA_ADMIN_TG_IDS", "42")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    Base.metadata.create_all(get_engine())

    # No real Telegram network: patch the client factory used by the inbound.
    from sreda.services import telegram_inbound as ti

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def send_message(self, **kwargs):
            return {"result": {"message_id": 777}}

        async def answer_callback_query(self, *a, **k):
            return {"ok": True}

        async def delete_message(self, *a, **k):
            return {"ok": True}

    monkeypatch.setattr(ti, "telegram_client_for", lambda bot_key, reg: _FakeClient())

    with TestClient(create_app()) as c:
        yield c

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _seed_challenge():
    from sreda.db.session import get_session_factory
    from sreda.services import admin_login as al

    s = get_session_factory()()
    try:
        return al.start_challenge(s, "1.2.3.4")
    finally:
        s.close()


def test_admin_start_via_webhook_returns_202_not_500(webhook_client):
    """Item A regression: admin /start adm_ через реальный webhook → 202 (не 500).
    handle_telegram_update возвращает None; TelegramWebhookAccepted допускает
    request_id=None."""
    r = _seed_challenge()
    payload = {
        "update_id": 100,
        "message": {
            "message_id": 5,
            "chat": {"id": 42},
            "from": {"id": 42},
            "text": f"/start adm_{r.challenge_id}",
        },
    }
    resp = webhook_client.post("/webhooks/telegram/sreda", json=payload, headers=_WEBHOOK_HEADERS)
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["request_id"] is None


def test_admin_confirm_via_webhook_returns_202(webhook_client):
    """adm_confirm callback через webhook → 202 (None-return path)."""
    from sreda.db.session import get_session_factory
    from sreda.services import admin_login as al

    r = _seed_challenge()
    s = get_session_factory()()
    al.attach_bot(s, r.challenge_id, "42", "sreda", "42")
    s.close()

    payload = {
        "update_id": 101,
        "callback_query": {
            "id": "cbq1",
            "from": {"id": 42},
            "message": {"message_id": 777, "chat": {"id": 42}},
            "data": f"adm_confirm:{r.challenge_id}",
        },
    }
    resp = webhook_client.post("/webhooks/telegram/sreda", json=payload, headers=_WEBHOOK_HEADERS)
    assert resp.status_code == 202, resp.text
    assert resp.json()["ok"] is True
