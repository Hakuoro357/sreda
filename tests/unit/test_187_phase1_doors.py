"""#187 Phase 1 — ingress doors отсечения soft-deleted тенанта.

Принцип: resolve → gate → provision. На входе (TG / MAX / queue-диспетчер)
СНАЧАЛА чистый резолв account→tenant (read-only, без side-effects), затем
``is_tenant_active``; удалённый существующий тенант → ТИХИЙ no-op (нет ensure,
нет welcome, нет LLM/STT/ответа). Новый юзер (резолв=None, тенанта ещё нет) →
гард ПРОПУСКАЕТСЯ, идёт обычная регистрация.

Чеклист приёмки #187:
- A1  test_deleted_inbound_silent  (TG + MAX)
- A1' новый-юзер не дропается гардом
- A13 test_dispatcher_gate_before_ensure
- A17 test_deleted_precedence (deleted > suspended — тихо, не UPGRADE_COPY)

Харнесс заимствован из ``test_telegram_long_poll.py`` (фикстура ``fresh_db``
+ ``seed_telegram_user``) и ``test_max_dispatch_and_wiring.py`` (MAX payload).
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from sreda.config.settings import get_settings
from sreda.db.base import Base
import sreda.db.models  # noqa: F401 — register all model classes on Base.metadata
from sreda.db.models.core import InboundMessage, Tenant
from sreda.db.session import get_engine, get_session_factory
from tests.unit.conftest import seed_telegram_user

EXISTING_TG_CHAT_ID = "100000003"
EXISTING_MAX_ACCOUNT_ID = "40921122"
EXISTING_MAX_CHAT_ID = "320955459"


# ---- Fixtures ----------------------------------------------------------


@pytest.fixture
def fresh_db(monkeypatch, tmp_path: Path):
    """Empty SQLite DB wired through the same caches production reads.

    Mirror of ``test_telegram_long_poll.fresh_db``. Both ``handle_telegram_update``
    and ``handle_max_update`` read ``get_session_factory()`` internally, so a real
    on-disk SQLite DB is enough — no session injection needed.
    """
    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(
        b"0123456789abcdef0123456789abcdef"
    ).decode("ascii")
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


def _seed_max_user(
    session,
    *,
    tenant_id: str,
    max_account_id: str = EXISTING_MAX_ACCOUNT_ID,
    max_chat_id: str = EXISTING_MAX_CHAT_ID,
    approved: bool = True,
) -> None:
    """Seed a MAX-channel Tenant + Workspace + User."""
    from sreda.db.models.core import User, Workspace

    approved_at = datetime.now(timezone.utc) if approved else None
    session.add(Tenant(id=tenant_id, name="Max Tenant", approved_at=approved_at))
    session.add(Workspace(id=f"ws_{tenant_id}", tenant_id=tenant_id, name="Home"))
    session.add(
        User(
            id=f"user_{tenant_id}",
            tenant_id=tenant_id,
            max_account_id=max_account_id,
            max_chat_id=max_chat_id,
        )
    )


def _mark_deleted(session, tenant_id: str) -> None:
    tenant = session.get(Tenant, tenant_id)
    tenant.deleted_at = datetime.now(timezone.utc)
    session.commit()


def _tg_payload(chat_id: str, *, update_id: int = 7001, text: str = "привет") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "chat": {"id": int(chat_id), "type": "private"},
            "text": text,
        },
    }


def _max_payload_message(
    *,
    sender_user_id: int = int(EXISTING_MAX_ACCOUNT_ID),
    chat_id: int = int(EXISTING_MAX_CHAT_ID),
    text: str = "привет",
) -> dict:
    return {
        "update_type": "message_created",
        "timestamp": 1777907183208,
        "message": {
            "recipient": {"chat_id": chat_id, "chat_type": "dialog"},
            "body": {"mid": "mid.test", "seq": 1, "text": text},
            "sender": {
                "user_id": sender_user_id,
                "first_name": "X",
                "name": "X",
                "is_bot": False,
            },
        },
    }


# ---- A1: deleted inbound silent ----------------------------------------


@pytest.mark.asyncio
async def test_deleted_inbound_silent_telegram(fresh_db, monkeypatch):
    """A1 (TG). Удалённый тенант шлёт сообщение → None, send_message НЕ вызван,
    inbound НЕ персистнут, ensure/обработка не запущены."""
    from sreda.services import telegram_inbound as ti

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        seeded = seed_telegram_user(
            session, chat_id=EXISTING_TG_CHAT_ID, tenant_id="tg_deleted",
            workspace_id="ws_tg_deleted", user_id="u_tg_deleted",
        )
        session.commit()
        _mark_deleted(session, seeded.tenant_id)

    send_mock = AsyncMock()
    fake_client = MagicMock()
    fake_client.send_message = send_mock

    # Любая попытка onboard'инга/обработки = провал (гард должен стоять раньше).
    ensure_spy = MagicMock(side_effect=AssertionError("ensure must not run for deleted"))
    monkeypatch.setattr(ti, "ensure_telegram_user_bundle", ensure_spy)
    monkeypatch.setattr(ti, "telegram_client_for", lambda *a, **k: fake_client)

    result = await ti.handle_telegram_update(_tg_payload(EXISTING_TG_CHAT_ID))

    assert result is None
    send_mock.assert_not_called()
    ensure_spy.assert_not_called()

    with SessionLocal() as session:
        rows = session.query(InboundMessage).all()
        assert rows == []  # inbound НЕ персистнут


@pytest.mark.asyncio
async def test_deleted_inbound_silent_max(fresh_db, monkeypatch):
    """A1 (MAX). Удалённый тенант шлёт сообщение → "", send_message НЕ вызван,
    inbound НЕ персистнут, ensure не запущен."""
    from sreda.services import max_inbound as mi

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        _seed_max_user(session, tenant_id="max_deleted")
        session.commit()
        _mark_deleted(session, "max_deleted")

    send_mock = AsyncMock()

    class _Client:
        def __init__(self, token: str) -> None:
            self.token = token

        async def send_message(self, **kwargs):
            return await send_mock(**kwargs)

    ensure_spy = MagicMock(side_effect=AssertionError("ensure must not run for deleted"))
    monkeypatch.setattr(mi, "ensure_max_user_bundle", ensure_spy)
    monkeypatch.setattr(mi, "MaxClient", _Client)

    result = await mi.handle_max_update(_max_payload_message())

    assert result == ""
    send_mock.assert_not_called()
    ensure_spy.assert_not_called()

    with SessionLocal() as session:
        rows = session.query(InboundMessage).all()
        assert rows == []


# ---- A1': new user not dropped by the guard ----------------------------


@pytest.mark.asyncio
async def test_new_user_not_dropped_by_guard_telegram(fresh_db, monkeypatch):
    """A1'. Незнакомый chat_id (резолв=None, тенанта нет) → гард ПРОПУСКАЕТСЯ,
    идёт обычный путь (ensure_telegram_user_bundle вызван)."""
    from sreda.services import telegram_inbound as ti

    ensure_called = {"n": 0}

    def fake_ensure(session, payload, *, bot_key="sreda"):
        ensure_called["n"] += 1
        # Сымитировать «странный»/pending исход без welcome, чтобы тест
        # не зависел от полного онбординга — достаточно факта вызова.
        raise _StopAfterEnsure()

    class _StopAfterEnsure(Exception):
        pass

    monkeypatch.setattr(ti, "ensure_telegram_user_bundle", fake_ensure)

    with pytest.raises(_StopAfterEnsure):
        await ti.handle_telegram_update(_tg_payload("999999999"))

    assert ensure_called["n"] == 1  # гард пропустил неизвестного → дошли до ensure


# ---- A13: queue-dispatcher gate before ensure --------------------------


@pytest.mark.asyncio
async def test_dispatcher_gate_before_ensure(fresh_db, monkeypatch):
    """A13. Job удалённого тенанта в диспетчере → гейт по job.tenant_id ДО
    ensure_*; job терминально закрыт (done), ensure/обработка НЕ вызваны."""
    from sreda.workers import message_dispatcher as md

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        seeded = seed_telegram_user(
            session, chat_id=EXISTING_TG_CHAT_ID, tenant_id="job_deleted",
            workspace_id="ws_job_deleted", user_id="u_job_deleted",
        )
        session.commit()
        _mark_deleted(session, seeded.tenant_id)

    ensure_spy = MagicMock(side_effect=AssertionError("ensure must not run for deleted"))
    turn_spy = AsyncMock(side_effect=AssertionError("turn must not run for deleted"))
    monkeypatch.setattr(md, "ensure_telegram_user_bundle", ensure_spy, raising=False)
    monkeypatch.setattr(md, "_process_approved_turn", turn_spy, raising=False)

    # Реальный pending-job удалённого тенанта в очереди.
    from sreda.workers.message_queue import enqueue_message
    with SessionLocal() as session:
        enqueue_message(
            session,
            tenant_id="job_deleted",
            thread_id="thread_job_deleted",  # гейт на tenant_id; thread_id для теста неважен
            channel="telegram",
            external_update_id="42",
            message_payload={
                "kind": "telegram_inbound",
                "bot_key": "sreda",
                "payload": _tg_payload(EXISTING_TG_CHAT_ID),
                "inbound_message_id": None,
            },
        )
        session.commit()

    # Реальный путь end-to-end: process_pending → claim → _dispatch_telegram →
    # гейт is_tenant_active → ранний return → mark_done. Пиним И no-ensure, И
    # терминальный done (Codex medium R1: A13 раньше не проверял терминальный статус).
    count = await md.process_pending(limit=5)
    assert count == 1

    ensure_spy.assert_not_called()
    turn_spy.assert_not_called()

    from sreda.db.models.message_jobs import MessageJob
    with SessionLocal() as session:
        job = session.query(MessageJob).one()
        assert job.status == "done", f"A13: job должен быть терминально done, got {job.status!r}"


# ---- A17: precedence deleted > suspended -------------------------------


@pytest.mark.asyncio
async def test_deleted_precedence_over_suspended_telegram(fresh_db, monkeypatch):
    """A17. Тенант удалён И без активной подписки → на входе ТИХО (нет ответа);
    suspended/UPGRADE_COPY НЕ отправляется (deleted-проверка ПЕРВОЙ, до gate)."""
    from sreda.services import telegram_inbound as ti

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        # Никакой подписки не заводим → EntitlementGate был бы "not allowed".
        seeded = seed_telegram_user(
            session, chat_id=EXISTING_TG_CHAT_ID, tenant_id="tg_del_susp",
            workspace_id="ws_tg_del_susp", user_id="u_tg_del_susp",
        )
        session.commit()
        _mark_deleted(session, seeded.tenant_id)

    send_mock = AsyncMock()
    fake_client = MagicMock()
    fake_client.send_message = send_mock
    monkeypatch.setattr(ti, "telegram_client_for", lambda *a, **k: fake_client)

    # EntitlementGate.check НЕ должен даже вызваться для удалённого.
    gate_cls = MagicMock(side_effect=AssertionError("gate must not run for deleted"))
    monkeypatch.setattr(
        "sreda.services.entitlement_gate.EntitlementGate", gate_cls, raising=False,
    )

    result = await ti.handle_telegram_update(_tg_payload(EXISTING_TG_CHAT_ID))

    assert result is None
    send_mock.assert_not_called()  # ни welcome, ни UPGRADE_COPY
