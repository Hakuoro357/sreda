"""#341 (F1 из аудита #336) — MAX webhook fail-open + перезапись max_chat_id.

CRITICAL security. Direction validated в
``plans/current-main-critical-audit-validation-r1.md`` §2 F1 / §5 test-matrix.

Механизм (код-гейт, НЕ промпт):
1. Route-level fail-closed (MAX): пустой secret + заданы token+url → 401.
2. Запрет перезаписи max_chat_id: NULL→value ок, non-NULL→другое запрещён.
3. Startup-гейт (lifespan): token+url заданы, secret пуст → старт падает.
4. Паритет TG: тот же route-level секрет-гейт (config-driven через
   telegram_webhook_url). chat_id-часть у TG отсутствует (identity-ключ).

Все чувствительные значения — вымышленные (фейковые токены/chat_id/account_id).
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models.audit import AuditLog  # noqa: F401 — register audit_log table
from sreda.db.models.channel_linking import (  # noqa: F401 — register table
    ChannelLinkToken,
)
from sreda.db.models.core import InboundMessage, Tenant, User, Workspace  # noqa: F401
from sreda.db.session import get_engine, get_session_factory
from sreda.main import create_app


# --- Вымышленные значения ---------------------------------------------------
FAKE_MAX_TOKEN = "fake-max-bot-token-341"
FAKE_MAX_WEBHOOK_URL = "https://bot.test.local/api/max/webhook"
FAKE_MAX_SECRET = "fake-max-secret-341"
FAKE_TG_TOKEN = "fake-tg-bot-token-341"
FAKE_TG_WEBHOOK_URL = "https://bot.test.local/webhooks/telegram/sreda"

# Атака: жертва с существующим max_chat_id, злоумышленник шлёт свой chat_id.
VICTIM_MAX_ACCOUNT_ID = "90000001"
VICTIM_OLD_CHAT_ID = "70000001"
ATTACKER_CHAT_ID = "70000009"


def _clear_caches() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _sqlite_env(monkeypatch, tmp_path, name: str) -> None:
    db_path = tmp_path / f"{name}.db"
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)


# ---------------------------------------------------------------------------
# Часть 3 — startup-гейт (lifespan)
# ---------------------------------------------------------------------------


def test_startup_fails_without_max_webhook_secret(monkeypatch, tmp_path) -> None:
    """token+url заданы, secret пуст → запуск (lifespan) падает."""
    _sqlite_env(monkeypatch, tmp_path, "startup_no_secret")
    monkeypatch.setenv("SREDA_MAX_BOT_TOKEN", FAKE_MAX_TOKEN)
    monkeypatch.setenv("SREDA_MAX_WEBHOOK_URL", FAKE_MAX_WEBHOOK_URL)
    monkeypatch.delenv("SREDA_MAX_WEBHOOK_SECRET_TOKEN", raising=False)
    _clear_caches()

    Base.metadata.create_all(get_engine())

    with pytest.raises(RuntimeError):
        with TestClient(create_app()):
            pass


def test_startup_ok_when_max_webhook_secret_present(monkeypatch, tmp_path) -> None:
    """Регрессия: token+url+secret заданы → lifespan НЕ падает по гейту."""
    _sqlite_env(monkeypatch, tmp_path, "startup_with_secret")
    monkeypatch.setenv("SREDA_MAX_BOT_TOKEN", FAKE_MAX_TOKEN)
    monkeypatch.setenv("SREDA_MAX_WEBHOOK_URL", FAKE_MAX_WEBHOOK_URL)
    monkeypatch.setenv("SREDA_MAX_WEBHOOK_SECRET_TOKEN", FAKE_MAX_SECRET)
    _clear_caches()

    Base.metadata.create_all(get_engine())

    # set_webhook уйдёт наружу — мокаем integration, чтобы lifespan прошёл
    # именно секрет-гейт (а не упал на сети). Гейт стоит ДО регистрации.
    async def _fake_set_webhook(self, *a, **kw):
        return {"ok": True}

    from sreda.integrations.max import MaxClient
    monkeypatch.setattr(MaxClient, "set_webhook", _fake_set_webhook)

    # Не должно бросить RuntimeError секрет-гейта.
    with TestClient(create_app()):
        pass


# ---------------------------------------------------------------------------
# Часть 1 — route-level fail-closed (MAX)
# ---------------------------------------------------------------------------


def test_max_webhook_rejects_when_token_url_set_but_secret_empty(
    monkeypatch, tmp_path
) -> None:
    """Дискриминатор = связка token+url (НЕ «prod»). Пустой secret + token+url
    → внешний POST отклонён (401), handle_max_update НЕ вызван."""
    _sqlite_env(monkeypatch, tmp_path, "route_no_secret")
    monkeypatch.setenv("SREDA_MAX_BOT_TOKEN", FAKE_MAX_TOKEN)
    monkeypatch.setenv("SREDA_MAX_WEBHOOK_URL", FAKE_MAX_WEBHOOK_URL)
    monkeypatch.delenv("SREDA_MAX_WEBHOOK_SECRET_TOKEN", raising=False)
    _clear_caches()

    Base.metadata.create_all(get_engine())

    called: list[bool] = []

    async def _spy_handle(*a, **kw):
        called.append(True)
        return "in_should_not_happen"

    monkeypatch.setattr(
        "sreda.api.routes.max_webhook.handle_max_update", _spy_handle
    )

    # TestClient без context-manager → lifespan (startup-гейт) НЕ запускается,
    # проверяем именно route-level гейт.
    client = TestClient(create_app())
    resp = client.post("/api/max/webhook", json={"update_type": "message_created"})

    assert resp.status_code == 401
    assert called == [], "handle_max_update не должен вызываться при fail-closed"


def test_forged_max_webhook_rejected(monkeypatch, tmp_path) -> None:
    """Secret настроен, но запрос без валидного X-Max-Bot-Api-Secret → 401."""
    _sqlite_env(monkeypatch, tmp_path, "route_forged")
    monkeypatch.setenv("SREDA_MAX_BOT_TOKEN", FAKE_MAX_TOKEN)
    monkeypatch.setenv("SREDA_MAX_WEBHOOK_URL", FAKE_MAX_WEBHOOK_URL)
    monkeypatch.setenv("SREDA_MAX_WEBHOOK_SECRET_TOKEN", FAKE_MAX_SECRET)
    _clear_caches()

    Base.metadata.create_all(get_engine())

    called: list[bool] = []

    async def _spy_handle(*a, **kw):
        called.append(True)
        return "in_should_not_happen"

    monkeypatch.setattr(
        "sreda.api.routes.max_webhook.handle_max_update", _spy_handle
    )

    client = TestClient(create_app())
    # Без заголовка вовсе.
    resp_missing = client.post(
        "/api/max/webhook", json={"update_type": "message_created"}
    )
    # С неверным секретом.
    resp_wrong = client.post(
        "/api/max/webhook",
        json={"update_type": "message_created"},
        headers={"X-Max-Bot-Api-Secret": "attacker-guess"},
    )

    assert resp_missing.status_code == 401
    assert resp_wrong.status_code == 401
    assert called == []


def test_max_webhook_accepts_with_matching_secret(monkeypatch, tmp_path) -> None:
    """Регрессия: валидный секрет → запрос проходит route-гейт (handle вызван)."""
    _sqlite_env(monkeypatch, tmp_path, "route_ok")
    monkeypatch.setenv("SREDA_MAX_BOT_TOKEN", FAKE_MAX_TOKEN)
    monkeypatch.setenv("SREDA_MAX_WEBHOOK_URL", FAKE_MAX_WEBHOOK_URL)
    monkeypatch.setenv("SREDA_MAX_WEBHOOK_SECRET_TOKEN", FAKE_MAX_SECRET)
    _clear_caches()

    Base.metadata.create_all(get_engine())

    called: list[bool] = []

    async def _spy_handle(*a, **kw):
        called.append(True)
        return "in_ok"

    monkeypatch.setattr(
        "sreda.api.routes.max_webhook.handle_max_update", _spy_handle
    )

    client = TestClient(create_app())
    resp = client.post(
        "/api/max/webhook",
        json={"update_type": "message_created"},
        headers={"X-Max-Bot-Api-Secret": FAKE_MAX_SECRET},
    )

    assert resp.status_code == 202
    assert called == [True]


# ---------------------------------------------------------------------------
# Часть 2 — запрет перезаписи max_chat_id (ассерт на СОСТОЯНИЕ ДАННЫХ)
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    try:
        yield sess
    finally:
        sess.close()


def test_inbound_does_not_overwrite_existing_max_chat_id(db_session) -> None:
    """Атака: юзер с non-NULL max_chat_id; inbound с ДРУГИМ chat_id НЕ меняет
    users.max_chat_id (иначе перехват уведомлений жертвы)."""
    from sreda.services.onboarding import ensure_max_user_bundle

    db_session.add(Tenant(id="tenant_max_victim", name="Victim"))
    db_session.add(User(
        id="user_max_victim",
        tenant_id="tenant_max_victim",
        max_account_id=VICTIM_MAX_ACCOUNT_ID,
        max_chat_id=VICTIM_OLD_CHAT_ID,
    ))
    db_session.commit()

    ensure_max_user_bundle(
        db_session,
        max_account_id=VICTIM_MAX_ACCOUNT_ID,
        max_chat_id=ATTACKER_CHAT_ID,
    )

    db_session.expire_all()
    user = db_session.get(User, "user_max_victim")
    assert user.max_chat_id == VICTIM_OLD_CHAT_ID, (
        "inbound не должен перезаписывать существующий max_chat_id"
    )


def test_inbound_populates_null_max_chat_id_but_not_overwrites(db_session) -> None:
    """NULL→value первичная установка РАЗРЕШЕНА; повторный inbound с ДРУГИМ
    chat_id уже НЕ перезаписывает."""
    from sreda.services.onboarding import ensure_max_user_bundle

    db_session.add(Tenant(id="tenant_max_n", name="N"))
    db_session.add(User(
        id="user_max_n",
        tenant_id="tenant_max_n",
        max_account_id="90000002",
        max_chat_id=None,
    ))
    db_session.commit()

    # NULL → value: устанавливается.
    ensure_max_user_bundle(
        db_session, max_account_id="90000002", max_chat_id="70000100",
    )
    db_session.expire_all()
    assert db_session.get(User, "user_max_n").max_chat_id == "70000100"

    # non-NULL → другое: НЕ перезаписывается.
    ensure_max_user_bundle(
        db_session, max_account_id="90000002", max_chat_id="70000200",
    )
    db_session.expire_all()
    assert db_session.get(User, "user_max_n").max_chat_id == "70000100"


def test_legitimate_channel_link_still_rebinds(db_session) -> None:
    """Аутентифицированный channel-link (channel_linking.consume_link) по-прежнему
    пишет/меняет max_chat_id — этот путь НЕ задет guard'ом onboarding'а."""
    from sreda.services.channel_linking import consume_link, start_link

    # Юзер зарегистрирован в TG; max_account_id ещё NULL, но по какой-то причине
    # уже есть старый max_chat_id — доказываем, что аутентиф. путь его меняет
    # (в отличие от inbound, который existing chat_id не трогает).
    db_session.add(Tenant(id="t_link", name="Link"))
    db_session.add(User(
        id="u_link",
        tenant_id="t_link",
        telegram_account_id="100500",
        max_chat_id="old_chat_link",
    ))
    db_session.commit()

    started = start_link(
        db_session, tenant_id="t_link", source_channel="telegram",
        source_user_id="u_link",
    )
    outcome = consume_link(
        db_session,
        raw_token=started.raw_token,
        target_channel="max",
        target_account_id="555111",
        target_chat_id="new_chat_link",
    )

    assert outcome.success is True
    db_session.expire_all()
    user = db_session.get(User, "u_link")
    assert user.max_account_id == "555111"
    assert user.max_chat_id == "new_chat_link", (
        "аутентифицированный channel-link должен менять max_chat_id"
    )


# ---------------------------------------------------------------------------
# Часть 4 — паритет Telegram (route-level, config-driven через webhook_url)
# ---------------------------------------------------------------------------


def test_forged_telegram_webhook_rejected(monkeypatch, tmp_path) -> None:
    """TG-паритет: заданы telegram_bot_token+telegram_webhook_url, secret пуст →
    внешний POST на TG-webhook отклонён (401)."""
    _sqlite_env(monkeypatch, tmp_path, "tg_route_no_secret")
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", FAKE_TG_TOKEN)
    monkeypatch.setenv("SREDA_TELEGRAM_WEBHOOK_URL", FAKE_TG_WEBHOOK_URL)
    monkeypatch.delenv("SREDA_TELEGRAM_WEBHOOK_SECRET_TOKEN", raising=False)
    _clear_caches()

    Base.metadata.create_all(get_engine())

    called: list[bool] = []

    async def _spy_handle(*a, **kw):
        called.append(True)
        return "in_should_not_happen"

    monkeypatch.setattr(
        "sreda.api.routes.telegram_webhook.handle_telegram_update", _spy_handle
    )

    client = TestClient(create_app())
    resp = client.post(
        "/webhooks/telegram/sreda",
        json={"update_id": 1, "message": {"message_id": 1,
              "chat": {"id": 123, "type": "private"}, "text": "hi"}},
    )

    assert resp.status_code == 401
    assert called == []

    session = get_session_factory()()
    try:
        assert session.query(InboundMessage).count() == 0
    finally:
        session.close()


def test_telegram_webhook_dev_fallback_without_url(monkeypatch, tmp_path) -> None:
    """Регрессия: bot_token задан, но webhook_url НЕ задан (long-poll deploy) →
    dev-fallback сохраняется, TG-webhook принимает без секрета (не 401 от гейта)."""
    _sqlite_env(monkeypatch, tmp_path, "tg_longpoll_fallback")
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", FAKE_TG_TOKEN)
    monkeypatch.delenv("SREDA_TELEGRAM_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("SREDA_TELEGRAM_WEBHOOK_SECRET_TOKEN", raising=False)
    _clear_caches()

    Base.metadata.create_all(get_engine())

    called: list[bool] = []

    async def _spy_handle(*a, **kw):
        called.append(True)
        return "in_ok"

    monkeypatch.setattr(
        "sreda.api.routes.telegram_webhook.handle_telegram_update", _spy_handle
    )

    client = TestClient(create_app())
    resp = client.post(
        "/webhooks/telegram/sreda",
        json={"update_id": 2, "message": {"message_id": 2,
              "chat": {"id": 123, "type": "private"}, "text": "hi"}},
    )

    # Гейт НЕ должен отклонить (webhook_url не задан) — доходит до handler.
    assert resp.status_code == 202
    assert called == [True]
