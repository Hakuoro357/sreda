"""#187 Phase 4a — входные поверхности периметра soft-delete тенанта.

Двери, добиваемые в 4a (триггеры self/admin + аудит — это Фаза 4b):

1. Mini-app auth (оба пути: ``_require_miniapp_auth`` через зависимость на ВСЕХ
   ``/miniapp/api/v1/*`` + ``_resolve_platform_auth`` channel-link) → удалённый
   тенант → **HTTP 410 Gone** reason ``tenant_deleted`` (НЕ 401/404 — иначе фронт
   уходит в re-provision-петлю), без мутаций.
2. ``consume_link`` (services/channel_linking.py) — он сжигает токен (UPDATE
   used_at) РАНЬШЕ чтения tenant. Пред-чтение ``token→source_tenant`` +
   ``is_tenant_active`` ДО сжигания: удалён → отклонён, ``used_at`` НЕ выставлен,
   source_user НЕ мутирован.
3. MAX link-ветки (services/max_inbound.py): ``bot_started lnk_*`` /
   ``confirm_link:`` → удалённый source-тенант → no-op (без consume/привязки/
   отправки confirm).

Анти-over-reach: для активного тенанта все пути работают как раньше.

Харнесс заимствован из ``test_miniapp_api.py`` (TestClient + signed initData),
``test_miniapp_channel_link_endpoints.py`` (FakeRequest + _patch_auth) и
``test_187_phase1_doors.py`` (fresh_db + get_session_factory для MAX).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import Headers, QueryParams

from sreda.db.base import Base
import sreda.db.models  # noqa: F401 — register model classes on Base.metadata
from sreda.db.models.channel_linking import ChannelLinkToken
from sreda.db.models.core import Tenant, User
from sreda.services.channel_linking import consume_link, start_link


# ===========================================================================
# Part 1 — Mini-app auth (_require_miniapp_auth) via the dependency → 410
# ===========================================================================

BOT_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
SEED_TG_ID = 352612382


def _make_init_data(
    *,
    bot_token: str = BOT_TOKEN,
    user_id: int = SEED_TG_ID,
    first_name: str = "Test",
    username: str = "testuser",
    auth_date: int | None = None,
) -> str:
    if auth_date is None:
        auth_date = int(time.time())
    user_json = json.dumps(
        {"id": user_id, "first_name": first_name, "username": username},
        separators=(",", ":"),
    )
    params: dict[str, str] = {"auth_date": str(auth_date), "user": user_json}
    sorted_pairs = sorted(params.items(), key=lambda p: p[0])
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_pairs)
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    params["hash"] = computed_hash
    return urlencode(params)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("SREDA_CONNECT_PUBLIC_BASE_URL", "https://connect.test.local")

    from fastapi.testclient import TestClient

    from sreda.api.deps import reset_rate_limiters
    from sreda.config.settings import get_settings
    from sreda.db.session import get_engine, get_session_factory
    from sreda.main import create_app

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    reset_rate_limiters()

    Base.metadata.create_all(get_engine())

    with TestClient(create_app()) as c:
        yield c

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    reset_rate_limiters()


def _seed_tg_tenant(*, deleted: bool) -> None:
    from sreda.db.repositories.seed import SeedRepository
    from sreda.db.session import get_session_factory

    session = get_session_factory()()
    try:
        SeedRepository(session).ensure_tenant_bundle(
            tenant_id="tenant_test",
            tenant_name="Test User",
            workspace_id="ws_test",
            workspace_name="Test",
            user_id="user_test",
            telegram_account_id=str(SEED_TG_ID),
            assistant_id="assistant_test",
            assistant_name="Среда",
        )
        if deleted:
            session.get(Tenant, "tenant_test").deleted_at = datetime.now(timezone.utc)
        session.commit()
    finally:
        session.close()


def test_miniapp_active_tenant_summary_200(client):
    """Анти-over-reach: активный тенант → /api/v1/summary работает (200)."""
    _seed_tg_tenant(deleted=False)
    resp = client.get(
        "/miniapp/api/v1/summary",
        headers={"Authorization": f"tma {_make_init_data()}"},
    )
    assert resp.status_code == 200


def test_miniapp_deleted_tenant_summary_410(client):
    """A4. Удалённый тенант → /api/v1/summary → 410 (НЕ 401/404),
    reason tenant_deleted."""
    _seed_tg_tenant(deleted=True)
    resp = client.get(
        "/miniapp/api/v1/summary",
        headers={"Authorization": f"tma {_make_init_data()}"},
    )
    assert resp.status_code == 410, resp.text
    assert resp.json()["detail"] == "tenant_deleted"


def test_miniapp_deleted_tenant_mutating_endpoint_410_no_mutation(client):
    """A4. Удалённый тенант → POST мутирующего эндпойнта (/shopping) → 410
    через ту же зависимость; ничего не записано (gate ДО хендлера)."""
    _seed_tg_tenant(deleted=True)
    resp = client.post(
        "/miniapp/api/v1/shopping",
        json={"title": "молоко"},
        headers={"Authorization": f"tma {_make_init_data()}"},
    )
    assert resp.status_code == 410, resp.text
    assert resp.json()["detail"] == "tenant_deleted"

    from sreda.db.models.housewife_food import ShoppingListItem
    from sreda.db.session import get_session_factory

    session = get_session_factory()()
    try:
        assert session.query(ShoppingListItem).count() == 0
    finally:
        session.close()


# ===========================================================================
# Part 2 — channel-link auth path (_resolve_platform_auth → endpoints) → 410
# ===========================================================================


@pytest.fixture()
def db_session(monkeypatch):
    # #138 Ф5-5c: резолв/gate/провижн открывают свои privileged/tenant сессии;
    # стабаем обе на эту sqlite-сессию (soft-delete гейт под ctx видит свой тенант).
    # ТОЛЬКО для db_session-тестов — client-based тесты идут на app-БД (get_engine).
    from contextlib import contextmanager

    import sreda.db.session as dbs

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()

    @contextmanager
    def _stub(arg):
        yield sess

    monkeypatch.setattr(dbs, "privileged_session", _stub)
    monkeypatch.setattr(dbs, "tenant_session", _stub)
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
        "home_bot_token": None,
        "home_bot_username": None,
        "home_miniapp_shortname": None,
        "home_bot_signup_open": True,
        "system_default_bot_key": "sreda",
        "admin_bot_key": "sreda",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _patch_settings(monkeypatch, **overrides) -> None:
    from sreda.api.routes import miniapp as mi

    monkeypatch.setattr(mi, "get_settings", lambda: _settings(**overrides))


def _patch_auth(monkeypatch, *, platform: str, payload: dict) -> None:
    """Stub initData validation in _resolve_platform_auth, BUT keep the real
    tenant resolve + 410 gate. We monkeypatch the validators it calls so the
    deleted-tenant gate (added in 4a) runs against the real db_session."""
    from sreda.api.routes import miniapp as mi

    if platform == "telegram":
        tg_user = SimpleNamespace(
            telegram_id=payload["telegram_id"],
            first_name="X",
            username=None,
            start_param=None,
        )
        monkeypatch.setattr(
            mi, "validate_telegram_init_data_any_bot",
            lambda raw, reg: ("sreda", tg_user),
        )
        monkeypatch.setattr(
            mi, "resolve_tenant_from_telegram_id",
            lambda session, tid: (payload["tenant_id"], payload["user_id"])
            if payload.get("tenant_id")
            else None,
        )
        monkeypatch.setattr(
            mi, "TelegramBotRegistry",
            SimpleNamespace(from_settings=lambda s: SimpleNamespace()),
        )


@pytest.mark.asyncio
async def test_channel_link_deleted_tenant_410(db_session, monkeypatch):
    """A4 (channel-link auth path). Удалённый тенант → channel-link эндпойнт
    через _resolve_platform_auth → 410, без мутации токена."""
    from sreda.api.routes import miniapp as mi

    db_session.get(Tenant, "tenant_tg").deleted_at = datetime.now(timezone.utc)
    db_session.commit()

    _patch_settings(monkeypatch)
    _patch_auth(
        monkeypatch,
        platform="telegram",
        payload={"telegram_id": "100", "user_id": "user_tg", "tenant_id": "tenant_tg"},
    )

    with pytest.raises(HTTPException) as exc:
        await mi.channel_link_account_status(
            FakeRequest(platform="telegram"), session=db_session,
        )
    assert exc.value.status_code == 410
    assert exc.value.detail == "tenant_deleted"


@pytest.mark.asyncio
async def test_channel_link_active_tenant_ok(db_session, monkeypatch):
    """Анти-over-reach: активный тенант → channel-link account-status работает."""
    from sreda.api.routes import miniapp as mi

    _patch_settings(monkeypatch)
    _patch_auth(
        monkeypatch,
        platform="telegram",
        payload={"telegram_id": "100", "user_id": "user_tg", "tenant_id": "tenant_tg"},
    )

    resp = await mi.channel_link_account_status(
        FakeRequest(platform="telegram"), session=db_session,
    )
    assert resp == {"linked": False, "target_channel": "max"}


# ===========================================================================
# Part 3 — consume_link pre-read gate (channel_linking.py)
# ===========================================================================


def test_consume_link_after_delete_rejected_no_mutation(db_session):
    """consume_link: токен выпущен (тенант активен) → тенант удалён →
    consume отклонён error=tenant_deleted, used_at НЕ выставлен, source_user
    НЕ привязан."""
    result = start_link(
        db_session,
        tenant_id="tenant_tg",
        source_channel="telegram",
        source_user_id="user_tg",
    )
    # Now delete the source tenant.
    db_session.get(Tenant, "tenant_tg").deleted_at = datetime.now(timezone.utc)
    db_session.commit()

    outcome = consume_link(
        db_session,
        raw_token=result.raw_token,
        target_channel="max",
        target_account_id="300",
        target_chat_id="chat_300",
    )

    assert outcome.success is False
    assert outcome.error == "tenant_deleted"

    db_session.expire_all()
    token_row = db_session.get(ChannelLinkToken, result.id)
    assert token_row.used_at is None, "токен НЕ должен быть сожжён"
    user = db_session.get(User, "user_tg")
    assert user.max_account_id is None, "source_user НЕ должен быть привязан"


def test_consume_link_active_tenant_succeeds(db_session):
    """Анти-over-reach: активный тенант → consume_link успешно привязывает."""
    result = start_link(
        db_session,
        tenant_id="tenant_tg",
        source_channel="telegram",
        source_user_id="user_tg",
    )
    outcome = consume_link(
        db_session,
        raw_token=result.raw_token,
        target_channel="max",
        target_account_id="300",
        target_chat_id="chat_300",
    )
    assert outcome.success is True
    db_session.expire_all()
    assert db_session.get(User, "user_tg").max_account_id == "300"
    assert db_session.get(ChannelLinkToken, result.id).used_at is not None


# ===========================================================================
# Part 4 — MAX link branches (max_inbound.py)
# ===========================================================================


@pytest.fixture
def fresh_db(monkeypatch, tmp_path: Path):
    """On-disk SQLite wired through prod caches — handle_max_update / link
    handlers read get_session_factory() internally."""
    from sreda.config.settings import get_settings
    from sreda.db.session import get_engine, get_session_factory

    db_path = tmp_path / "max.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("SREDA_MAX_BOT_TOKEN", "max-token")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _seed_link_token_and_user(*, deleted: bool) -> str:
    """Seed a TG source tenant+user, issue a link token (target=max), then
    optionally mark the tenant deleted. Returns the raw token."""
    from sreda.db.session import get_session_factory

    session = get_session_factory()()
    try:
        session.add(Tenant(id="tenant_src", name="Src"))
        session.add(
            User(id="user_src", tenant_id="tenant_src", telegram_account_id="555")
        )
        session.commit()
        result = start_link(
            session,
            tenant_id="tenant_src",
            source_channel="telegram",
            source_user_id="user_src",
        )
        if deleted:
            session.get(Tenant, "tenant_src").deleted_at = datetime.now(timezone.utc)
            session.commit()
        return result.raw_token
    finally:
        session.close()


@pytest.mark.asyncio
async def test_max_link_confirm_deleted_tenant_noop(fresh_db, monkeypatch):
    """MAX confirm_link: source-тенант удалён → no-op (consume НЕ вызван,
    привязки нет, used_at не выставлен)."""
    from sreda.services import max_inbound as mi

    raw_token = _seed_link_token_and_user(deleted=True)

    send_mock = AsyncMock()
    answer_mock = AsyncMock()

    class _Client:
        def __init__(self, token: str) -> None:
            self.token = token

        async def send_message(self, **kwargs):
            return await send_mock(**kwargs)

        async def answer_callback(self, *a, **k):
            return await answer_mock(*a, **k)

    monkeypatch.setattr(mi, "MaxClient", _Client)
    consume_spy = MagicMock(side_effect=AssertionError("consume must not run for deleted"))
    monkeypatch.setattr(
        "sreda.services.channel_linking.consume_link", consume_spy, raising=True,
    )

    await mi._handle_max_link_confirm_cb(
        raw_token=raw_token,
        sender_user_id="999",
        chat_id="chat_999",
        callback_id="cb1",
    )

    consume_spy.assert_not_called()

    from sreda.db.session import get_session_factory

    session = get_session_factory()()
    try:
        token = (
            session.query(ChannelLinkToken)
            .filter(ChannelLinkToken.tenant_id == "tenant_src")
            .one()
        )
        assert token.used_at is None
    finally:
        session.close()


@pytest.mark.asyncio
async def test_max_link_start_deleted_tenant_no_confirm(fresh_db, monkeypatch):
    """MAX bot_started lnk_*: source-тенант удалён → confirm-кнопка НЕ
    отправляется (silent no-op)."""
    from sreda.services import max_inbound as mi

    raw_token = _seed_link_token_and_user(deleted=True)

    send_mock = AsyncMock()

    class _Client:
        def __init__(self, token: str) -> None:
            self.token = token

        async def send_message(self, **kwargs):
            return await send_mock(**kwargs)

    monkeypatch.setattr(mi, "MaxClient", _Client)

    await mi._handle_max_link_start_cmd(raw_token=raw_token, chat_id="chat_999")

    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_max_link_confirm_active_tenant_links(fresh_db, monkeypatch):
    """Анти-over-reach: активный source-тенант → confirm_link привязывает MAX."""
    from sreda.services import max_inbound as mi

    raw_token = _seed_link_token_and_user(deleted=False)

    answer_mock = AsyncMock()

    class _Client:
        def __init__(self, token: str) -> None:
            self.token = token

        async def send_message(self, **kwargs):
            return None

        async def answer_callback(self, *a, **k):
            return await answer_mock(*a, **k)

    monkeypatch.setattr(mi, "MaxClient", _Client)

    await mi._handle_max_link_confirm_cb(
        raw_token=raw_token,
        sender_user_id="777",
        chat_id="chat_777",
        callback_id="cb1",
    )

    from sreda.db.session import get_session_factory

    session = get_session_factory()()
    try:
        user = session.get(User, "user_src")
        assert user.max_account_id == "777"
    finally:
        session.close()


@pytest.mark.asyncio
async def test_max_link_cancel_deleted_tenant_noop(fresh_db, monkeypatch):
    """ФИКС 3 (cancel-ветка). MAX cancel_link: source-тенант удалён → no-op
    (column-only пред-чтение ДО мутации): ``used_at`` НЕ выставлен, ранний
    return, ``answer_callback`` НЕ вызван."""
    from sreda.services import max_inbound as mi

    raw_token = _seed_link_token_and_user(deleted=True)

    answer_mock = AsyncMock()

    class _Client:
        def __init__(self, token: str) -> None:
            self.token = token

        async def answer_callback(self, *a, **k):
            return await answer_mock(*a, **k)

    monkeypatch.setattr(mi, "MaxClient", _Client)

    await mi._handle_max_link_cancel_cb(raw_token=raw_token, callback_id="cb1")

    answer_mock.assert_not_called()

    from sreda.db.session import get_session_factory

    session = get_session_factory()()
    try:
        token = (
            session.query(ChannelLinkToken)
            .filter(ChannelLinkToken.tenant_id == "tenant_src")
            .one()
        )
        assert token.used_at is None, "used_at НЕ должен быть выставлен"
    finally:
        session.close()


@pytest.mark.asyncio
async def test_max_link_cancel_active_tenant_works(fresh_db, monkeypatch):
    """Анти-over-reach (cancel-ветка). Активный source-тенант → cancel_link
    отрабатывает как раньше: ``used_at`` выставлен, ``answer_callback`` вызван."""
    from sreda.services import max_inbound as mi

    raw_token = _seed_link_token_and_user(deleted=False)

    answer_mock = AsyncMock()

    class _Client:
        def __init__(self, token: str) -> None:
            self.token = token

        async def answer_callback(self, *a, **k):
            return await answer_mock(*a, **k)

    monkeypatch.setattr(mi, "MaxClient", _Client)

    await mi._handle_max_link_cancel_cb(raw_token=raw_token, callback_id="cb1")

    answer_mock.assert_called_once()

    from sreda.db.session import get_session_factory

    session = get_session_factory()()
    try:
        token = (
            session.query(ChannelLinkToken)
            .filter(ChannelLinkToken.tenant_id == "tenant_src")
            .one()
        )
        assert token.used_at is not None, "used_at должен быть выставлен"
    finally:
        session.close()


# ===========================================================================
# Part 4b — FIX (R2 MAJOR): mini-app HTTP cancel endpoint SOURCE-tenant gate
# ===========================================================================
#
# ``POST /miniapp/api/v1/channel-link/cancel`` мутирует ``used_at`` БЕЗ гейта на
# source-тенанта токена: ``_resolve_platform_auth`` гейтит только TARGET
# (запрашивающего), НЕ владельца токена. Активный target мог бы отменить токен,
# чей source-тенант уже soft-deleted → durable-мутация для удалённого тенанта.
# Фикс: column-only пред-чтение source ``tenant_id`` по ``token_hash`` →
# ``is_tenant_active`` ДО мутации; удалён → {"ok": False} БЕЗ мутации.


def _patch_max_auth_active_target(monkeypatch) -> None:
    """Stub MAX initData validation so the TARGET (caller) resolves to the
    active ``tenant_max``/``user_max``. The real source-tenant gate in the
    cancel endpoint then runs against the real db_session.

    NB: ``_resolve_platform_auth`` re-imports ``validate_max_init_data`` LOCALLY
    from ``sreda.services.max_auth`` (not the module-level alias used by
    ``_require_miniapp_auth``), so we patch it at the SOURCE module.
    """
    import sreda.services.max_auth as max_auth
    from sreda.api.routes import miniapp as mi

    max_user = SimpleNamespace(
        max_user_id="200",
        max_chat_id="chat_200",
        first_name="X",
        username=None,
        start_param=None,
    )
    monkeypatch.setattr(max_auth, "validate_max_init_data", lambda raw, token: max_user)
    monkeypatch.setattr(
        mi, "resolve_tenant_from_max_account_id",
        lambda session, acc: ("tenant_max", "user_max"),
    )


@pytest.mark.asyncio
async def test_miniapp_channel_link_cancel_deleted_source_no_write(
    db_session, monkeypatch
):
    """R2 MAJOR. Токен выпущен активным source (tenant_tg, target=max) → source
    удалён → активный TARGET зовёт cancel-эндпойнт → ``used_at`` НЕ выставлен,
    отклонён ({"ok": False}). Без фикса cancel сжёг бы токен удалённого тенанта.
    """
    from sreda.api.routes import miniapp as mi

    # Issue a token while the source tenant is still active (target=max).
    result = start_link(
        db_session,
        tenant_id="tenant_tg",
        source_channel="telegram",
        source_user_id="user_tg",
    )
    # Delete the SOURCE tenant (the token owner).
    db_session.get(Tenant, "tenant_tg").deleted_at = datetime.now(timezone.utc)
    db_session.commit()

    _patch_settings(monkeypatch)
    _patch_max_auth_active_target(monkeypatch)

    resp = await mi.channel_link_cancel(
        FakeRequest(platform="max", body={"raw_token": result.raw_token}),
        session=db_session,
    )
    assert resp == {"ok": False}

    db_session.expire_all()
    token_row = db_session.get(ChannelLinkToken, result.id)
    assert token_row.used_at is None, (
        "used_at НЕ должен быть выставлен для удалённого source-тенанта"
    )


@pytest.mark.asyncio
async def test_miniapp_channel_link_cancel_active_source_works(
    db_session, monkeypatch
):
    """Анти-over-reach. Активный source (tenant_tg) → активный TARGET зовёт
    cancel → ``used_at`` выставлен, {"ok": True}. Нейтрализация гарда не должна
    менять этот путь."""
    from sreda.api.routes import miniapp as mi

    result = start_link(
        db_session,
        tenant_id="tenant_tg",
        source_channel="telegram",
        source_user_id="user_tg",
    )

    _patch_settings(monkeypatch)
    _patch_max_auth_active_target(monkeypatch)

    resp = await mi.channel_link_cancel(
        FakeRequest(platform="max", body={"raw_token": result.raw_token}),
        session=db_session,
    )
    assert resp == {"ok": True}

    db_session.expire_all()
    token_row = db_session.get(ChannelLinkToken, result.id)
    assert token_row.used_at is not None, "used_at должен быть выставлен"


# ===========================================================================
# Part 5 — FIX 1: mini-app MAX existing-tenant gate ДО max_chat_id-мутации
# ===========================================================================


def test_miniapp_deleted_max_user_chat_id_change_410_no_write(
    db_session, monkeypatch
):
    """ФИКС 1 (MAX-ветка). Удалённый existing MAX-юзер открывает mini-app со
    СМЕНОЙ max_chat_id → 410 ``tenant_deleted`` И запись max_chat_id НЕ
    произошла (гейт ДО durable-мутации auth-слоя).

    Без фикса: гейт стоял ПОСЛЕ refresh'а → ``user_row.max_chat_id`` уже
    закоммичен для удалённого тенанта."""
    from sreda.api.routes import miniapp as mi

    # Mark the MAX tenant deleted; existing user has max_chat_id="chat_200".
    db_session.get(Tenant, "tenant_max").deleted_at = datetime.now(timezone.utc)
    db_session.commit()

    # initData validation → existing MAX user with a NEW chat_id.
    max_user = SimpleNamespace(
        max_user_id="200",
        max_chat_id="chat_NEW_999",
        first_name="X",
        username=None,
        start_param=None,
    )
    monkeypatch.setattr(
        mi, "validate_max_init_data", lambda raw, token: max_user,
    )
    monkeypatch.setattr(
        mi, "resolve_tenant_from_max_account_id",
        lambda session, acc: ("tenant_max", "user_max"),
    )
    _patch_settings(monkeypatch)

    request = FakeRequest(platform="max")
    bg = SimpleNamespace(add_task=lambda *a, **k: None)

    with pytest.raises(HTTPException) as exc:
        mi._require_miniapp_auth(request, bg, session=db_session)
    assert exc.value.status_code == 410
    assert exc.value.detail == "tenant_deleted"

    db_session.expire_all()
    user = db_session.get(User, "user_max")
    assert user.max_chat_id == "chat_200", (
        "max_chat_id НЕ должен быть перезаписан для удалённого тенанта"
    )
