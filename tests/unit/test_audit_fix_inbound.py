"""Регрессионные тесты аудита 2026-07-18 — FixWorker «inbound».

Покрывает точечные фиксы в inbound-scope:

- svc-inbound #2: TG-dedup ДО создания SecureRecord (нет сиротских PII-строк);
- svc-inbound #3: MAX duplicate short-circuit ПЕРЕД welcome-consume (нет стомпа
  ``processing_status`` существующей строки);
- svc-inbound #4: TG reminder callback — cross-tenant отказ без мутации;
- svc-inbound #5: message_callback без ``callback.user`` → явный None (не bot id);
- svc-inbound #6: ``consume_link`` с битым токеном → outcome 'invalid_token_format';
- svc-inbound #1 / FC-4: ``consume_link`` — IntegrityError-recovery на проигранной
  гонке attach (громкий отказ вместо тихого дубля / 500);
- svc-inbound #7: ``_ADMIN_DENY_LAST`` — cap + eviction;
- cross-concurrency Н1 (FC-1): post-turn 'processed' ТОЛЬКО при успешном ходе
  (TG react safe-fallback; MAX failed-джоб);
- cross-latency NEW-1 / svc-features #2: call-site'ы ffprobe идут через
  ``probe_audio_async`` (async-обёртка от features-воркера, контракт FC-audio);
- шов #133: inbound-модули не вызывают raw ``asyncio.create_task``.

Все тесты без сети и без PG (SQLite / fakes).
"""

from __future__ import annotations

import base64
import importlib
import inspect
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from sreda.config.settings import get_settings
from sreda.db.base import Base
import sreda.db.models  # noqa: F401 — register all model classes on Base.metadata
from sreda.db.models.core import InboundMessage, SecureRecord, Tenant, User, Workspace
from sreda.db.session import get_engine, get_session_factory
from sreda.services.channel_linking import consume_link
from sreda.services.inbound_messages import (
    _extract_max_sender_user_id,
    persist_telegram_inbound_event,
)
from tests.unit.conftest import seed_telegram_user


# ---------------------------------------------------------------------------
# svc-inbound #2 — dedup ДО SecureRecord (TG)
# ---------------------------------------------------------------------------


@pytest.fixture
def lite_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="tenant_1", name="Test"))
    sess.add(Workspace(id="ws_1", tenant_id="tenant_1", name="Default"))
    sess.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100"))
    sess.commit()
    yield sess
    sess.close()


def _tg_payload(update_id: int, text: str = "привет") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "chat": {"id": 100, "type": "private"},
            "text": text,
        },
    }


def test_tg_duplicate_does_not_create_orphan_secure_record(lite_session):
    """Retry/ределивери того же update_id: dedup срабатывает ДО store_secure_json —
    вторая зашифрованная PII-строка НЕ создаётся (зеркало MAX-порядка)."""
    first = persist_telegram_inbound_event(
        lite_session, bot_key="sreda", payload=_tg_payload(42),
    )
    second = persist_telegram_inbound_event(
        lite_session, bot_key="sreda", payload=_tg_payload(42),
    )
    assert first.is_duplicate is False
    assert second.is_duplicate is True
    assert second.inbound_message_id == first.inbound_message_id
    assert lite_session.query(InboundMessage).count() == 1
    # До фикса на каждый дубликат создавалась НОВАЯ сиротская SecureRecord.
    assert lite_session.query(SecureRecord).count() == 1


def test_tg_fresh_update_still_creates_secure_record(lite_session):
    """НЕ-вакуум: свежий update по-прежнему получает secure_record + inbound."""
    result = persist_telegram_inbound_event(
        lite_session, bot_key="sreda", payload=_tg_payload(43),
    )
    assert result.is_duplicate is False
    assert lite_session.query(InboundMessage).count() == 1
    assert lite_session.query(SecureRecord).count() == 1
    row = lite_session.query(InboundMessage).one()
    assert row.secure_record_id is not None


# ---------------------------------------------------------------------------
# svc-inbound #5 — message_callback без callback.user → None (не bot id)
# ---------------------------------------------------------------------------


def test_max_callback_without_user_returns_none_not_bot_id():
    """Callback без ``callback.user``: явный fail (caller дропнет update),
    а НЕ fallthrough в ``message.sender`` (там БОТ — путь инцидента
    tenant_max_290524257)."""
    payload = {
        "update_type": "message_callback",
        "callback": {"callback_id": "cb1", "payload": "rem_done:rid-1"},
        "message": {"sender": {"user_id": 290524257}},  # БОТ, автор кнопочного сообщения
    }
    assert _extract_max_sender_user_id(payload) is None


def test_max_callback_with_user_returns_user_id():
    payload = {
        "update_type": "message_callback",
        "callback": {"callback_id": "cb1", "user": {"user_id": 40921122}},
        "message": {"sender": {"user_id": 290524257}},
    }
    assert _extract_max_sender_user_id(payload) == 40921122


def test_max_message_created_still_uses_message_sender():
    """НЕ-вакуум: обычное сообщение резолвится через message.sender (прежнее)."""
    payload = {
        "update_type": "message_created",
        "timestamp": 1777907183208,
        "message": {"sender": {"user_id": 40921122}},
    }
    assert _extract_max_sender_user_id(payload) == 40921122


def test_max_bot_started_still_uses_payload_user():
    payload = {"update_type": "bot_started", "user": {"user_id": 40921122}}
    assert _extract_max_sender_user_id(payload) == 40921122


# ---------------------------------------------------------------------------
# svc-inbound #6 — consume_link: битый токен → outcome, не raise
# ---------------------------------------------------------------------------


def test_consume_link_invalid_token_format_returns_outcome_not_raise():
    """MAX call-site вызывает consume_link без try/except: искажённый токен
    обязан вернуть outcome 'invalid_token_format', а не ValueError → 500."""
    outcome = consume_link(
        None,  # сессия на этой ветке не трогается (возврат до любого SQL)
        raw_token="broken token with spaces!!",
        target_channel="max",
        target_account_id="123",
        target_chat_id="1",
    )
    assert outcome.success is False
    assert outcome.error == "invalid_token_format"


# ---------------------------------------------------------------------------
# svc-inbound #1 / FC-4 — consume_link: IntegrityError-recovery на гонке
# ---------------------------------------------------------------------------


class _Scalar:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):  # noqa: ANN001
        return self._value


class _ConsumeRaceSession:
    """Session-double для consume_link: отдаёт очередь результатов на execute,
    commit падает IntegrityError (проигранная гонка attach, DB-unique сработал)."""

    def __init__(self, results) -> None:
        self._results = deque(results)
        self.commits = 0
        self.rollbacks = 0
        self.added: list = []

    def execute(self, statement, params=None):  # noqa: ANN001
        return _Scalar(self._results.popleft())

    def add(self, obj) -> None:  # noqa: ANN001
        self.added.append(obj)

    def commit(self) -> None:
        self.commits += 1
        raise IntegrityError(
            "UPDATE users SET max_account_id=%(incoming)s", {},
            Exception("UNIQUE constraint failed: users.max_account_id"),
        )

    def rollback(self) -> None:
        self.rollbacks += 1


def _consume_race_results(*, racer):
    """Очередь результатов execute для MAX-target пути consume_link:
    pre-read tenant → UPDATE...RETURNING → source_user → collision#1 →
    (commit падает, rollback) → collision#2."""
    token_row = SimpleNamespace(
        id="link_1", source_user_id="u1",
        source_channel="telegram", target_channel="max",
    )
    source_user = SimpleNamespace(
        id="u1", tenant_id="t1",
        max_account_id=None, max_chat_id=None, telegram_account_id=None,
    )
    return [None, token_row, source_user, None, racer]


def test_consume_link_lost_race_returns_collision_outcome():
    """IntegrityError на финальном commit (гонку выиграл параллельный attach) →
    rollback + перечитанная коллизия → штатный outcome, НЕ 500."""
    racer = SimpleNamespace(id="u2", tenant_id="t2")
    session = _ConsumeRaceSession(_consume_race_results(racer=racer))
    outcome = consume_link(
        session,
        raw_token="A" * 43,  # валидный формат (urlsafe alphabet)
        target_channel="max",
        target_account_id="999",
        target_chat_id="chat_1",
    )
    assert session.commits == 1
    assert session.rollbacks == 1
    assert outcome.success is False
    assert outcome.error == "account_already_registered_separately"
    assert outcome.tenant_id == "t1"


def test_consume_link_lost_race_same_tenant_returns_family_outcome():
    racer = SimpleNamespace(id="u2", tenant_id="t1")  # тот же tenant
    session = _ConsumeRaceSession(_consume_race_results(racer=racer))
    outcome = consume_link(
        session,
        raw_token="A" * 43,
        target_channel="max",
        target_account_id="999",
        target_chat_id="chat_1",
    )
    assert outcome.success is False
    assert outcome.error == "account_belongs_to_other_family_member"


def test_consume_link_integrity_error_without_collision_reraises():
    """IntegrityError БЕЗ видимой коллизии после rollback — чужая
    integrity-ошибка: НЕ маскируем outcome'ом, пробрасываем."""
    session = _ConsumeRaceSession(_consume_race_results(racer=None))
    with pytest.raises(IntegrityError):
        consume_link(
            session,
            raw_token="A" * 43,
            target_channel="max",
            target_account_id="999",
            target_chat_id="chat_1",
        )
    assert session.rollbacks == 1


# ---------------------------------------------------------------------------
# svc-inbound #4 — TG reminder callback: cross-tenant отказ
# ---------------------------------------------------------------------------


class _FakeTelegramClient:
    def __init__(self) -> None:
        self.answered: list[str] = []
        self.edited: list[str] = []

    async def answer_callback_query(self, callback_id, text=None, **kw):  # noqa: ANN001
        self.answered.append(str(text))
        return {"ok": True}

    async def edit_message_text(self, **kw):  # noqa: ANN001
        self.edited.append("edited")
        return {"ok": True}


class _RecSession:
    """commit/rollback-рекордер (на cross-tenant отказе commit НЕ вызывается)."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _NoopReminderService:
    def __init__(self, session) -> None:  # noqa: ANN001
        pass

    def acknowledge(self, reminder) -> None:  # noqa: ANN001
        pass

    def snooze(self, reminder, minutes=0) -> None:  # noqa: ANN001
        pass


def _reminder_callback_query() -> dict:
    return {
        "id": "cb1",
        "message": {"chat": {"id": "1"}, "message_id": 5, "text": "🔔 Полить цветы"},
    }


@pytest.mark.asyncio
async def test_tg_reminder_callback_cross_tenant_refused_without_mutation(monkeypatch):
    """Напоминание чужого тенанта: отказ «не ваше», rollback (снять FOR UPDATE
    до сети), НИКАКИХ acknowledge/snooze/commit (зеркало MAX-версии)."""
    import sreda.services.housewife_reminders as _hr
    from sreda.services.telegram_bot import _handle_reminder_callback

    reminder = SimpleNamespace(id="rid-1", tenant_id="t2")
    monkeypatch.setattr(
        _hr, "read_reminder_for_callback", lambda *a, **k: reminder,
    )

    class _ExplodingService:
        def __init__(self, session):  # noqa: ANN001
            raise AssertionError("service must not run on cross-tenant refusal")

    monkeypatch.setattr(_hr, "HousewifeReminderService", _ExplodingService)

    session = _RecSession()
    fake = _FakeTelegramClient()
    await _handle_reminder_callback(
        session=session,
        telegram_client=fake,
        callback_query=_reminder_callback_query(),
        data="rem_done:rid-1",
        onboarding=SimpleNamespace(tenant_id="t1"),
    )
    assert fake.answered == ["Это напоминание не ваше."]
    assert session.rollbacks == 1  # FOR UPDATE отпущен ДО сетевого ответа
    assert session.commits == 0
    assert fake.edited == []


@pytest.mark.asyncio
async def test_tg_reminder_callback_same_tenant_still_works(monkeypatch):
    """НЕ-вакуум: свой тенант — обычный ack/commit/edit (проверка прозрачна)."""
    import sreda.services.housewife_reminders as _hr
    from sreda.services.telegram_bot import _handle_reminder_callback

    reminder = SimpleNamespace(id="rid-1", tenant_id="t1")
    monkeypatch.setattr(
        _hr, "read_reminder_for_callback", lambda *a, **k: reminder,
    )
    monkeypatch.setattr(_hr, "HousewifeReminderService", _NoopReminderService)

    session = _RecSession()
    fake = _FakeTelegramClient()
    await _handle_reminder_callback(
        session=session,
        telegram_client=fake,
        callback_query=_reminder_callback_query(),
        data="rem_done:rid-1",
        onboarding=SimpleNamespace(tenant_id="t1"),
    )
    assert session.commits == 1
    assert fake.answered == ["Принято ✅"]
    assert fake.edited == ["edited"]


# ---------------------------------------------------------------------------
# svc-inbound #7 — _ADMIN_DENY_LAST: cap + eviction
# ---------------------------------------------------------------------------


def test_admin_deny_throttle_cap_and_eviction(monkeypatch):
    import sreda.services.telegram_inbound as ti

    store: dict[str, float] = {}
    monkeypatch.setattr(ti, "_ADMIN_DENY_LAST", store)
    cap = ti._ADMIN_DENY_MAX_ENTRIES
    now = 1000.0

    # Протухшие (старше окна) записи вытесняются первыми.
    for i in range(cap):
        store[f"stale_{i}"] = now - 100.0
    assert ti._admin_deny_should_reply("new_id", now=now) is True
    assert len(store) <= cap
    assert "new_id" in store

    # Все свежие на капе → вытесняется самая старая, таблица не растёт.
    store.clear()
    for i in range(cap):
        store[f"fresh_{i}"] = now - 1.0
    assert ti._admin_deny_should_reply("brand_new", now=now) is True
    assert len(store) == cap
    assert "brand_new" in store

    # Сам throttle не сломан: повтор в окне → молчим.
    assert ti._admin_deny_should_reply("brand_new", now=now + 1) is False


# ---------------------------------------------------------------------------
# Шов #133 + контракт probe_audio_async (source-scan, как test_seams)
# ---------------------------------------------------------------------------


def test_inbound_modules_have_no_raw_asyncio_create_task():
    """Регрессия: telegram_inbound использовал raw asyncio.create_task мимо
    локального шва ``_create_task`` (его патчит функциональный харнес)."""
    import sreda.services.max_inbound as mi
    import sreda.services.telegram_inbound as ti

    for mod in (ti, mi):
        calls = re.findall(r"asyncio\.create_task\(", inspect.getsource(mod))
        assert not calls, f"{mod.__name__}: задачи обязаны идти через _create_task"


def test_ffprobe_callsites_use_async_probe_wrapper():
    """cross-latency NEW-1 / svc-features #2: оба voice call-site'а (TG + MAX)
    зовут async-обёртку ``probe_audio_async`` (asyncio.to_thread внутри,
    контракт с features-воркером), а не sync ``ffprobe_duration`` на loop."""
    for mod_name in ("sreda.services.telegram_bot", "sreda.services.max_inbound"):
        src = inspect.getsource(importlib.import_module(mod_name))
        assert "ffprobe_duration(" not in src, mod_name
        assert "await probe_audio_async(" in src, mod_name


# ---------------------------------------------------------------------------
# fresh_db harness (зеркало test_187_phase2b_recheck) — on-disk SQLite через
# те же кэши (get_settings/get_engine/get_session_factory), что читает прод.
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db(monkeypatch, tmp_path: Path):
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


MAX_ACCOUNT_ID = "40921122"
MAX_CHAT_ID = "320955459"
TG_CHAT_ID = "100000003"


def _seed_max_user(session, *, tenant_id: str) -> None:
    session.add(
        Tenant(id=tenant_id, name="Max Tenant", approved_at=datetime.now(timezone.utc))
    )
    session.add(Workspace(id=f"ws_{tenant_id}", tenant_id=tenant_id, name="Home"))
    session.add(
        User(
            id=f"user_{tenant_id}",
            tenant_id=tenant_id,
            max_account_id=MAX_ACCOUNT_ID,
            max_chat_id=MAX_CHAT_ID,
        )
    )


def _max_text_payload(mid: str = "mid.test", text: str = "привет") -> dict:
    return {
        "update_type": "message_created",
        "timestamp": 1777907183208,
        "message": {
            "recipient": {"chat_id": int(MAX_CHAT_ID), "chat_type": "dialog"},
            "body": {"mid": mid, "seq": 1, "text": text},
            "sender": {
                "user_id": int(MAX_ACCOUNT_ID),
                "first_name": "X",
                "name": "X",
                "is_bot": False,
            },
        },
    }


def _tg_text_payload(text: str = "привет") -> dict:
    return {
        "update_id": 7101,
        "message": {
            "message_id": 1,
            "chat": {"id": int(TG_CHAT_ID), "type": "private"},
            "text": text,
        },
    }


# ---------------------------------------------------------------------------
# svc-inbound #3 — MAX: duplicate ПЕРЕД welcome-consume (нет стомпа статуса)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_duplicate_does_not_stomp_existing_row_status(fresh_db, monkeypatch):
    """Гонка первого контакта: обе доставки видят is_welcome_sent=False (мок),
    обе шлют welcome; вторая доставка — ДУБЛИКАТ (тот же body.mid). До фикса
    welcome-ветка шла ПЕРЕД duplicate и стомпила processing_status существующей
    строки в 'ignored'; после фикса duplicate short-circuit'ит первым."""
    from sreda.services import max_inbound as mi
    from sreda.services import onboarding as ob

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        _seed_max_user(session, tenant_id="max_dup")
        session.commit()

    send_mock = AsyncMock(return_value={})

    class _Client:
        def __init__(self, token: str) -> None:
            self.token = token

        async def send_message(self, **kwargs):  # noqa: ANN003
            return await send_mock(**kwargs)

    # Обе доставки «проиграли» check-then-send welcome (неатомарность самого
    # check-then-send — отдельный задокументированный пробел, не этот фикс).
    monkeypatch.setattr(ob, "is_welcome_sent", lambda *a, **k: False)
    monkeypatch.setattr(mi, "MaxClient", _Client)

    first_id = await mi.handle_max_update(_max_text_payload())
    assert first_id  # свежий ingest

    # Выигравший ход уже перевёл оригинальную строку дальше по жизненному циклу.
    with SessionLocal() as session:
        row = session.get(InboundMessage, first_id)
        row.processing_status = "processing_started"
        session.commit()

    second_id = await mi.handle_max_update(_max_text_payload())
    assert second_id == first_id  # дубликат резолвится в ту же строку

    with SessionLocal() as session:
        row = session.get(InboundMessage, first_id)
        assert row.processing_status == "processing_started", (
            "duplicate + welcome НЕ должен стомпить processing_status чужой строки"
        )


# ---------------------------------------------------------------------------
# FC-1 (cross-concurrency Н1) — TG react: 'processed' только при успешном ходе
# ---------------------------------------------------------------------------


def _tg_onboarding_stub(*, tenant_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        is_new_user=False,
        chat_id=TG_CHAT_ID,
        tenant_id=tenant_id,
        workspace_id=f"ws_{tenant_id}",
        user_id=f"user_{tenant_id}",
        assistant_id=None,
    )


def _patch_tg_react_path(monkeypatch, *, reply) -> MagicMock:
    """Общий харнес react-текстового хода TG: внешние зависимости замоканы,
    handle_turn возвращает заданный reply. Возвращает status_spy."""
    from sreda.runtime import react_loop
    from sreda.services import llm as llm_mod
    from sreda.services import telegram_inbound as ti
    from sreda.services import entitlement_gate as eg_mod

    status_spy = MagicMock()
    monkeypatch.setattr(ti, "_set_processing_status", status_spy)

    fake_client = MagicMock()
    fake_client.send_message = AsyncMock(return_value={"result": {"message_id": 1}})
    fake_client.edit_message_text = AsyncMock(return_value={})
    fake_client.delete_message = AsyncMock(return_value={})
    monkeypatch.setattr(ti, "telegram_client_for", lambda *a, **k: fake_client)

    monkeypatch.setattr(react_loop, "react_provider", lambda tenant_id: "test")
    monkeypatch.setattr(react_loop, "react_fallback_llm", lambda prov: None)
    monkeypatch.setattr(react_loop, "handle_turn", AsyncMock(return_value=reply))
    monkeypatch.setattr(react_loop, "spawn_post_turn_summary", lambda **k: None)
    monkeypatch.setattr(llm_mod, "get_chat_llm", lambda **k: MagicMock())
    monkeypatch.setattr(
        eg_mod, "EntitlementGate",
        lambda session: SimpleNamespace(
            check=lambda tenant_id: SimpleNamespace(
                plan_key="pro", is_grandfathered=False,
            ),
        ),
    )

    async def _noop_progress(*a, **k):
        return None

    monkeypatch.setattr(ti, "_drive_ack_progress", _noop_progress)
    return status_spy


@pytest.mark.asyncio
async def test_tg_react_internal_error_turn_is_not_marked_processed(fresh_db, monkeypatch):
    """Н1: handle_turn отдал safe-fallback (had_internal_error=True) — post-turn
    observability commit НЕ должен помечать inbound 'processed': статус остаётся
    для unprocessed_inbound monitor'а."""
    from sreda.runtime.react_loop import _Reply
    from sreda.services import telegram_inbound as ti

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        seed_telegram_user(
            session, chat_id=TG_CHAT_ID, tenant_id="tg_fc1",
            workspace_id="ws_tg_fc1", user_id="user_tg_fc1",
        )
        session.commit()

    monkeypatch.setenv("SREDA_REACT_LOOP_ENABLED_TENANTS", "tg_fc1")
    get_settings.cache_clear()

    reply = _Reply("Ой, я потеряла контекст этого диалога. Повтори, пожалуйста.")
    reply.had_internal_error = True  # контракт FC-1 с react-loop воркером
    status_spy = _patch_tg_react_path(monkeypatch, reply=reply)

    await ti._process_approved_turn_locked(
        bot_key="sreda",
        payload=_tg_text_payload(),
        onboarding=_tg_onboarding_stub(tenant_id="tg_fc1"),
        inbound_message_id="in_fc1_err",
    )

    statuses = [c.args[2] for c in status_spy.call_args_list if len(c.args) >= 3]
    assert "processing_started" in statuses
    assert "processed" not in statuses, (
        f"safe-fallback ход не должен помечаться 'processed': {statuses}"
    )


@pytest.mark.asyncio
async def test_tg_react_successful_turn_is_marked_processed(fresh_db, monkeypatch):
    """НЕ-вакуум: обычный react-ответ (без had_internal_error) — 'processed'
    выставляется как раньше (флаг нагружен: удали условие — красный первый тест)."""
    from sreda.runtime.react_loop import _Reply
    from sreda.services import telegram_inbound as ti

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        seed_telegram_user(
            session, chat_id=TG_CHAT_ID, tenant_id="tg_fc1",
            workspace_id="ws_tg_fc1", user_id="user_tg_fc1",
        )
        session.commit()

    monkeypatch.setenv("SREDA_REACT_LOOP_ENABLED_TENANTS", "tg_fc1")
    get_settings.cache_clear()

    status_spy = _patch_tg_react_path(monkeypatch, reply=_Reply("Готово."))

    await ti._process_approved_turn_locked(
        bot_key="sreda",
        payload=_tg_text_payload(),
        onboarding=_tg_onboarding_stub(tenant_id="tg_fc1"),
        inbound_message_id="in_fc1_ok",
    )

    statuses = [c.args[2] for c in status_spy.call_args_list if len(c.args) >= 3]
    assert "processed" in statuses


# ---------------------------------------------------------------------------
# FC-1 (cross-concurrency Н1) — MAX runtime: 'processed' только при completed
# ---------------------------------------------------------------------------


def _max_onboarding_stub(*, tenant_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        is_new_user=False,
        max_account_id=MAX_ACCOUNT_ID,
        max_chat_id=MAX_CHAT_ID,
        tenant_id=tenant_id,
        workspace_id=f"ws_{tenant_id}",
        user_id=f"user_{tenant_id}",
        assistant_id=None,
    )


def _patch_max_runtime_path(monkeypatch, *, job_outcome: str) -> MagicMock:
    from sreda.runtime import dispatcher, executor
    from sreda.services import max_inbound as mi

    status_spy = MagicMock()
    monkeypatch.setattr(mi, "_set_processing_status", status_spy)
    monkeypatch.setattr(mi, "MaxClient", lambda *a, **k: MagicMock())
    monkeypatch.setattr(
        mi, "_maybe_transcribe_max_voice",
        AsyncMock(side_effect=lambda payload, **k: payload),
    )
    monkeypatch.setattr(mi, "_send_max_ack", AsyncMock(return_value=None))
    monkeypatch.setattr(mi, "_wait_ack_then_delete", AsyncMock(return_value=None))
    monkeypatch.setattr(
        dispatcher, "dispatch_max_action",
        MagicMock(return_value=SimpleNamespace(name="fake_action")),
    )

    class _FakeRuntime:
        def __init__(self, *a, **k) -> None:
            pass

        def enqueue_action(self, action):  # noqa: ANN001
            return SimpleNamespace(job_id="job_1")

        async def process_job(self, job_id):  # noqa: ANN001
            return job_outcome

    monkeypatch.setattr(executor, "ActionRuntimeService", _FakeRuntime)
    return status_spy


@pytest.mark.asyncio
async def test_max_failed_job_is_not_marked_processed(fresh_db, monkeypatch):
    """Н1: process_job вернул 'failed' (сам не бросает) — inbound НЕ 'processed',
    строка остаётся 'processing_started' для unprocessed_inbound monitor'а."""
    from sreda.services import max_inbound as mi

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        _seed_max_user(session, tenant_id="max_fc1")
        session.commit()

    status_spy = _patch_max_runtime_path(monkeypatch, job_outcome="failed")

    await mi._process_approved_max_turn(
        bot_key="sreda",
        payload=_max_text_payload(),
        onboarding=_max_onboarding_stub(tenant_id="max_fc1"),
        inbound_message_id="in_max_fc1_fail",
    )

    statuses = [c.args[2] for c in status_spy.call_args_list if len(c.args) >= 3]
    assert "processing_started" in statuses
    assert "processed" not in statuses, (
        f"failed-джоб не должен помечаться 'processed': {statuses}"
    )


@pytest.mark.asyncio
async def test_max_completed_job_is_marked_processed(fresh_db, monkeypatch):
    """НЕ-вакуум: process_job → 'completed' — 'processed' выставляется."""
    from sreda.services import max_inbound as mi

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        _seed_max_user(session, tenant_id="max_fc1")
        session.commit()

    status_spy = _patch_max_runtime_path(monkeypatch, job_outcome="completed")

    await mi._process_approved_max_turn(
        bot_key="sreda",
        payload=_max_text_payload(),
        onboarding=_max_onboarding_stub(tenant_id="max_fc1"),
        inbound_message_id="in_max_fc1_ok",
    )

    statuses = [c.args[2] for c in status_spy.call_args_list if len(c.args) >= 3]
    assert "processed" in statuses
