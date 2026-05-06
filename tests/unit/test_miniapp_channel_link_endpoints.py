"""Phase 4 channel-linking endpoint tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import Headers, QueryParams

from sreda.db.base import Base
from sreda.db.models.channel_linking import ChannelLinkToken
from sreda.db.models.core import Tenant, User
from sreda.services.channel_linking import consume_link, start_link


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="tenant_tg", name="TG Tenant"))
    sess.add(Tenant(id="tenant_max", name="MAX Tenant"))
    sess.add(User(id="user_tg", tenant_id="tenant_tg", telegram_account_id="100"))
    sess.add(
        User(
            id="user_max",
            tenant_id="tenant_max",
            max_account_id="200",
            max_chat_id="chat_200",
        )
    )
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


class FakeRequest:
    def __init__(self, *, platform: str, body: dict | None = None):
        self.query_params = QueryParams(f"platform={platform}")
        self.headers = Headers({
            "authorization": "tma fake_init_data",
            "content-type": "application/json",
        })
        self._body = body or {}

    async def json(self) -> dict:
        return self._body


def _settings(**overrides):
    defaults = {
        "telegram_bot_token": "TG_TOKEN",
        "max_bot_token": "MAX_TOKEN",
        "telegram_bot_username": "sreda01_bot",
        "telegram_miniapp_shortname": "sreda_app",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _patch_settings(monkeypatch, **overrides) -> None:
    from sreda.api.routes import miniapp as mi

    monkeypatch.setattr(mi, "get_settings", lambda: _settings(**overrides))


def _patch_auth(monkeypatch, *, platform: str, payload: dict) -> None:
    from sreda.api.routes import miniapp as mi

    def fake_auth(request, settings, session=None):
        return platform, payload

    monkeypatch.setattr(mi, "_resolve_platform_auth", fake_auth)


@pytest.mark.asyncio
async def test_start_validates_tg_settings(db_session, monkeypatch):
    from sreda.api.routes import miniapp as mi

    _patch_settings(
        monkeypatch,
        telegram_bot_username=None,
        telegram_miniapp_shortname=None,
    )
    _patch_auth(
        monkeypatch,
        platform="max",
        payload={"max_user_id": "200", "max_chat_id": "chat_200"},
    )

    with pytest.raises(HTTPException) as exc:
        await mi.channel_link_start(FakeRequest(platform="max"), session=db_session)

    assert exc.value.status_code == 500
    assert "tg_bot_username" in str(exc.value.detail)
    assert "tg_miniapp_shortname" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_consume_endpoint_rejects_client_supplied_target(db_session, monkeypatch):
    from sreda.api.routes import miniapp as mi

    _patch_settings(monkeypatch)
    result = start_link(
        db_session,
        tenant_id="tenant_tg",
        source_channel="telegram",
        source_user_id="user_tg",
    )
    # Use 300 (no collision in fixture — fixture has user_max with 200).
    # The test verifies that even if client sends "hacker" в body, server
    # derives target from validated initData payload (300) and ignores body.
    _patch_auth(
        monkeypatch,
        platform="max",
        payload={"max_user_id": "300", "max_chat_id": "chat_300"},
    )

    response = await mi.channel_link_consume(
        FakeRequest(
            platform="max",
            body={"raw_token": result.raw_token, "target_account_id": "hacker"},
        ),
        session=db_session,
    )

    assert response["ok"] is True
    assert response["tenant_id"] == "tenant_tg"
    db_session.expire_all()
    user = db_session.get(User, "user_tg")
    assert user.max_account_id == "300"  # from initData, not "hacker"
    assert user.max_chat_id == "chat_300"


@pytest.mark.asyncio
async def test_consume_endpoint_invalid_token_400(db_session, monkeypatch):
    from sreda.api.routes import miniapp as mi

    _patch_settings(monkeypatch)
    _patch_auth(
        monkeypatch,
        platform="max",
        payload={"max_user_id": "200", "max_chat_id": "chat_200"},
    )

    with pytest.raises(HTTPException) as exc:
        await mi.channel_link_consume(
            FakeRequest(platform="max", body={"raw_token": "garbage"}),
            session=db_session,
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "invalid_token_format"


@pytest.mark.asyncio
async def test_cancel_invalidates_token(db_session, monkeypatch):
    from sreda.api.routes import miniapp as mi

    _patch_settings(monkeypatch)
    result = start_link(
        db_session,
        tenant_id="tenant_tg",
        source_channel="telegram",
        source_user_id="user_tg",
    )
    _patch_auth(
        monkeypatch,
        platform="max",
        payload={"max_user_id": "200", "max_chat_id": "chat_200"},
    )

    response = await mi.channel_link_cancel(
        FakeRequest(platform="max", body={"raw_token": result.raw_token}),
        session=db_session,
    )

    assert response == {"ok": True}
    row = db_session.get(ChannelLinkToken, result.id)
    assert row.used_at is not None
    outcome = consume_link(
        db_session,
        raw_token=result.raw_token,
        target_channel="max",
        target_account_id="200",
        target_chat_id="chat_200",
    )
    assert outcome.success is False
    assert outcome.error == "not_found_or_expired"


@pytest.mark.asyncio
async def test_cancel_wrong_channel_noop(db_session, monkeypatch):
    from sreda.api.routes import miniapp as mi

    _patch_settings(monkeypatch)
    result = start_link(
        db_session,
        tenant_id="tenant_tg",
        source_channel="telegram",
        source_user_id="user_tg",
    )
    _patch_auth(
        monkeypatch,
        platform="telegram",
        payload={"telegram_id": "100", "user_id": "user_tg"},
    )

    response = await mi.channel_link_cancel(
        FakeRequest(platform="telegram", body={"raw_token": result.raw_token}),
        session=db_session,
    )

    assert response == {"ok": False}
    row = db_session.get(ChannelLinkToken, result.id)
    assert row.used_at is None


@pytest.mark.asyncio
async def test_account_status_tg_uses_hash(db_session, monkeypatch):
    from sreda.api.routes import miniapp as mi

    _patch_settings(monkeypatch)
    _patch_auth(
        monkeypatch,
        platform="telegram",
        payload={"telegram_id": "100", "user_id": "user_tg"},
    )
    db_session.get(User, "user_tg").max_account_id = "200"
    db_session.commit()

    response = await mi.channel_link_account_status(
        FakeRequest(platform="telegram"),
        session=db_session,
    )

    assert response == {"linked": True, "target_channel": "max"}


@pytest.mark.asyncio
async def test_account_status_max_target_tg_uses_hash(db_session, monkeypatch):
    from sreda.api.routes import miniapp as mi

    _patch_settings(monkeypatch)
    _patch_auth(
        monkeypatch,
        platform="max",
        payload={"max_user_id": "200", "max_chat_id": "chat_200"},
    )
    user = db_session.get(User, "user_max")
    user.telegram_account_id = "999"
    db_session.flush()
    user.tg_account_hash = None
    db_session.commit()

    response = await mi.channel_link_account_status(
        FakeRequest(platform="max"),
        session=db_session,
    )

    assert response == {"linked": False, "target_channel": "telegram"}
