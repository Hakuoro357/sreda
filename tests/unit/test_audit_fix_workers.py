"""Регрессионные тесты фиксов аудита 2026-07-18 (воркер «workers»).

Покрывает находки workers-review.md:
  #1 telegram_long_poll.py — poison-update: dead-letter после MAX_UPDATE_ATTEMPTS
     + admin alert, офсет больше не блокируется head-of-line.
  #2 outbox_delivery.py — attempts + потолок ретраев + dead-letter
     (перманентный 4xx → failed сразу; транзиент → failed на потолке).
  #3 skill_platform_processor.py — rollback в except перед recovery-записями
     (класс инцидента #331).
  #4 job_runner.py — пер-воркерная изоляция ошибок в тике.
  #5 scheduler.py/sender.py — мёртвые стабы удалены.
  #6 retention_cleanup.py — total включает plan_library_entries.
  #7 housewife_reminder_worker.py — reminder без канала НЕ помечается fired.
  #8 proactive_events.py — sync-хендлер уходит в asyncio.to_thread.

Без сети и без PG (файловая SQLite через фикстуру worker_db / локальную
poller_db), по образцу соседних тестов.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session as _SA_Session

from sreda.config.settings import get_settings
from sreda.db.base import Base
import sreda.db.models  # noqa: F401 — register all model classes on Base.metadata
from sreda.db.models.core import Job, OutboxMessage, Tenant, User, Workspace
from sreda.db.models.housewife import FamilyReminder
from sreda.db.models.poller_state import PollerOffset
from sreda.db.models.skill_platform import SkillRun, SkillRunAttempt
from sreda.db.session import get_engine, get_session_factory
from sreda.integrations.max.client import MaxDeliveryError
from sreda.integrations.telegram.client import TelegramDeliveryError
from sreda.maintenance.retention_cleanup import RetentionCleanupResult
from sreda.services.housewife_reminders import HousewifeReminderService
from sreda.workers import job_runner as jr
from sreda.workers import outbox_delivery as od
from sreda.workers import telegram_long_poll as tlp
from sreda.workers.housewife_reminder_worker import (
    NO_CHANNEL_RETRY_MINUTES,
    HousewifeReminderWorker,
)
from sreda.workers.job_runner import _run_worker_isolated
from sreda.workers.outbox_delivery import OutboxDeliveryWorker
from sreda.workers.telegram_long_poll import TelegramLongPoller


# ---------------------------------------------------------------------------
# Общие фикстуры/хелперы
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_delivery_attempts():
    """Изоляция in-memory счётчика попыток outbox между тестами."""
    od._DELIVERY_ATTEMPTS.clear()
    yield
    od._DELIVERY_ATTEMPTS.clear()


@pytest.fixture
def poller_db(monkeypatch, tmp_path: Path):
    """Пустая файловая SQLite + env для поллера (по образцу
    test_telegram_long_poll.fresh_db)."""
    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(
        b"0123456789abcdef0123456789abcdef"
    ).decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "test-token")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _make_poller() -> TelegramLongPoller:
    poller = TelegramLongPoller("test-token")
    poller._lock_conn = MagicMock()
    poller._lock_engine = MagicMock()
    return poller


class _FetchScript:
    """Скриптованный _fetch_updates: отдаёт батчи по очереди, затем
    паркуется на asyncio.sleep, чтобы cancel() имел точку доставки."""

    def __init__(self, batches: list[list[dict]]):
        self.batches = list(batches)

    async def __call__(self):
        if self.batches:
            return self.batches.pop(0)
        await asyncio.sleep(60)
        return []


async def _run_then_cancel(poller: TelegramLongPoller, *, settle: float = 0.5) -> None:
    run_task = asyncio.create_task(poller.run_forever())
    try:
        await asyncio.sleep(settle)
    finally:
        run_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, BaseException):
            pass


def _alert_spy(monkeypatch) -> list:
    """Переопределяет no-op autouse-фикстуру: собирает вызовы
    send_admin_alert (ленивый импорт в проде резолвится на вызове)."""
    calls: list = []
    monkeypatch.setattr(
        "sreda.services.admin_alerts.send_admin_alert",
        lambda *a, **kw: calls.append((a, kw)),
    )
    return calls


# ---------------------------------------------------------------------------
# #1 telegram_long_poll — poison-update dead-letter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poison_update_dead_lettered_after_max_attempts(
    poller_db, monkeypatch, tmp_path
):
    """Update, детерминированно роняющий handler MAX_UPDATE_ATTEMPTS раз,
    dead-letter'ится: offset продвигается мимо него, хвост батча обрабатывается,
    уходит admin alert (P0, dedupe по bot_key+update_id).

    C6/M14 (R1): счётчик durable (poller_offsets.poison_*), сырой «мёртвый»
    апдейт журналируется ДО сдвига offset."""
    monkeypatch.setattr(tlp, "BACKOFF_SECS", 0.01)  # ускоряем retry-цикл
    _journal = tmp_path / "poison.jsonl"
    monkeypatch.setattr(tlp, "_POISON_JOURNAL_PATH", str(_journal))
    alerts = _alert_spy(monkeypatch)
    poller = _make_poller()

    handled: list[int] = []

    async def handle(payload, *, bot_key="sreda"):
        handled.append(payload["update_id"])
        if payload["update_id"] == 7:
            raise RuntimeError("deterministic crash")
        return "ok"

    poison = {"update_id": 7, "message": {"chat": {"id": 1}, "text": "boom"}}
    good = {"update_id": 8, "message": {"chat": {"id": 1}, "text": "hi"}}
    # Каждая итерация возвращает poison первым (offset не продвинут) + хвост.
    poller._fetch_updates = _FetchScript([[poison, good]] * 3)  # type: ignore[assignment]
    with patch.object(tlp, "handle_telegram_update", handle):
        await _run_then_cancel(poller)

    # poison пытались ровно MAX_UPDATE_ATTEMPTS раз, затем dead-letter;
    # good обработан в той же итерации, где poison ушёл в dead-letter.
    assert handled.count(7) == tlp.MAX_UPDATE_ATTEMPTS
    assert 8 in handled

    SessionLocal = get_session_factory()
    with SessionLocal() as s:
        row = s.query(PollerOffset).filter_by(channel="telegram:sreda").first()
        assert row is not None
        assert row.last_update_id == 8  # offset продвинут МИМО poison
        # C6/M14: durable poison-счётчик сброшен после dead-letter.
        assert row.poison_count == 0
        assert row.poison_update_id is None
    assert poller.offset == 9

    # C6: сырой «мёртвый» апдейт durably зажурналирован ДО сдвига offset.
    lines = _journal.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["update_id"] == 7
    assert rec["update"]["message"]["text"] == "boom"  # восстановимо

    assert len(alerts) == 1
    args, kwargs = alerts[0]
    assert args[0] == "P0"
    assert "poison" in args[1]
    assert kwargs["dedupe_key"] == "telegram-poison-update:sreda:7"


@pytest.mark.asyncio
async def test_update_failure_recovers_when_handler_heals(
    poller_db, monkeypatch
):
    """Одиночный сбой (attempt 1 < MAX): offset НЕ продвигается, update
    повторяется; при успехе счётчик сбрасывается, alert НЕ уходит."""
    monkeypatch.setattr(tlp, "BACKOFF_SECS", 0.01)
    alerts = _alert_spy(monkeypatch)
    poller = _make_poller()

    calls: list[int] = []

    async def flaky(payload, *, bot_key="sreda"):
        calls.append(payload["update_id"])
        if len(calls) == 1:
            raise RuntimeError("transient crash")
        return "ok"

    upd = {"update_id": 11, "message": {"chat": {"id": 1}, "text": "x"}}
    poller._fetch_updates = _FetchScript([[upd], [upd]])  # type: ignore[assignment]
    with patch.object(tlp, "handle_telegram_update", flaky):
        await _run_then_cancel(poller)

    assert calls == [11, 11]  # один повтор после сбоя
    SessionLocal = get_session_factory()
    with SessionLocal() as s:
        row = s.query(PollerOffset).filter_by(channel="telegram:sreda").first()
        assert row is not None
        assert row.last_update_id == 11
        # C6/M14: успех сбрасывает durable poison-счётчик.
        assert row.poison_count == 0
        assert row.poison_update_id is None
    assert alerts == []


@pytest.mark.asyncio
async def test_poison_counter_durable_across_restart(
    poller_db, monkeypatch, tmp_path
):
    """M14 (R1): poison-счётчик переживает РЕСТАРТ процесса. Новый инстанс
    поллера видит накопленные попытки в БД, а не начинает с нуля — иначе
    детерминированно ядовитый апдейт при рестарте на каждом сбое НИКОГДА не
    достигал бы потолка и вечно блокировал очередь (in-memory dict терялся)."""
    monkeypatch.setattr(tlp, "BACKOFF_SECS", 0.01)
    monkeypatch.setattr(tlp, "_POISON_JOURNAL_PATH", str(tmp_path / "p.jsonl"))
    poison = {"update_id": 7, "message": {"chat": {"id": 1}, "text": "boom"}}

    async def crash(payload, *, bot_key="sreda"):
        raise RuntimeError("crash")

    SessionLocal = get_session_factory()

    # «Рестарт 1»: инстанс #1, один сбой (attempt 1 < MAX) — offset не двинут.
    p1 = _make_poller()
    p1._fetch_updates = _FetchScript([[poison]])  # type: ignore[assignment]
    with patch.object(tlp, "handle_telegram_update", crash):
        await _run_then_cancel(p1)
    with SessionLocal() as s:
        row = s.query(PollerOffset).filter_by(channel="telegram:sreda").first()
        assert row.poison_count == 1 and row.poison_update_id == 7
        assert row.last_update_id == 6  # якорь на update_id-1 (re-deliver poison)

    # «Рестарт 2»: НОВЫЙ инстанс (in-memory счётчик был бы 0). Второй сбой →
    # count=2 (durable, накопился ЧЕРЕЗ рестарт), а не сброшен в 1.
    p2 = _make_poller()
    p2._fetch_updates = _FetchScript([[poison]])  # type: ignore[assignment]
    with patch.object(tlp, "handle_telegram_update", crash):
        await _run_then_cancel(p2)
    with SessionLocal() as s:
        row = s.query(PollerOffset).filter_by(channel="telegram:sreda").first()
        assert row.poison_count == 2  # ← durable через рестарт (RED без фикса)
        assert row.last_update_id == 6  # всё ещё не продвинут (< MAX)


# ---------------------------------------------------------------------------
# #2 outbox_delivery — attempts + потолок + dead-letter
# ---------------------------------------------------------------------------


class _FailingTelegram:
    def __init__(self, exc: TelegramDeliveryError) -> None:
        self.exc = exc
        self.calls = 0

    async def send_message(self, **kwargs):
        self.calls += 1
        raise self.exc


class _OkTelegram:
    async def send_message(self, **kwargs):
        return {"ok": True}


class _FailingMax:
    def __init__(self, exc: MaxDeliveryError) -> None:
        self.exc = exc
        self.calls = 0

    async def send_message(self, *, recipient, text, format=None, attachments=None):
        self.calls += 1
        raise self.exc


def _seed_base(session) -> None:
    session.add(Tenant(id="t1", name="T"))
    session.add(Workspace(id="w1", tenant_id="t1", name="W"))
    session.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    session.commit()


def _tg_row(*, channel_type: str = "telegram") -> OutboxMessage:
    payload = {"chat_id": "42", "text": "hello", "reply_markup": None}
    if channel_type == "max":
        payload["chat_id"] = "max-chat"
    return OutboxMessage(
        id=f"out_{uuid4().hex[:16]}",
        tenant_id="t1",
        workspace_id="w1",
        user_id="u1",
        channel_type=channel_type,
        feature_key=None,
        is_interactive=True,
        status="pending",
        payload_json=json.dumps(payload),
    )


_NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def test_outbox_permanent_4xx_dead_letters_immediately(worker_db):
    """400 «Bad Request» (payload-перманент) → failed с ПЕРВОЙ попытки
    (бессмысленно ретраить тот же payload), drop_reason фиксирует причину,
    claim снят. Прим.: 401/403 БОЛЬШЕ НЕ перманентны (R1 M15 — состояние
    КАНАЛА, retryable; см. test_audit_fix_r1_privacy.py)."""
    _seed_base(worker_db)
    tg = _FailingTelegram(
        TelegramDeliveryError(
            "Bad Request: message text is empty",
            method="sendMessage",
            status_code=400,
        )
    )
    worker = OutboxDeliveryWorker(telegram_client=tg)
    row = _tg_row()
    worker_db.add(row)
    worker_db.commit()

    processed = asyncio.run(worker.process_pending_messages(now=_NOW))
    assert processed == 1

    worker_db.expire_all()
    row = worker_db.get(OutboxMessage, row.id)
    assert row.status == "failed"
    assert row.drop_reason == "delivery_permanent_400"
    assert row.claim_token is None
    assert row.lease_expires_at is None
    assert row.id not in od._DELIVERY_ATTEMPTS
    assert tg.calls == 1  # перманентную ошибку НЕ ретраим


def test_outbox_transient_error_keeps_pending_with_attempt_counter(worker_db):
    """Транзиентная ошибка (сеть/таймаут, status_code=None) → ретрай:
    строка pending, claim снят (быстрый повтор), счётчик = 1."""
    _seed_base(worker_db)
    tg = _FailingTelegram(TelegramDeliveryError("network timeout"))
    worker = OutboxDeliveryWorker(telegram_client=tg)
    row = _tg_row()
    worker_db.add(row)
    worker_db.commit()

    processed = asyncio.run(worker.process_pending_messages(now=_NOW))
    assert processed == 1

    worker_db.expire_all()
    row = worker_db.get(OutboxMessage, row.id)
    assert row.status == "pending"
    assert row.claim_token is None  # быстрый retry на следующем тике
    assert od._DELIVERY_ATTEMPTS[row.id] == 1


def test_outbox_transient_dead_letters_at_retry_ceiling(worker_db, monkeypatch):
    """Транзиентная ошибка на потолке попыток → dead-letter
    (failed/delivery_retry_exhausted) + admin alert + счётчик снят."""
    monkeypatch.setattr(od, "OUTBOX_MAX_DELIVERY_ATTEMPTS", 2)
    alerts = _alert_spy(monkeypatch)
    _seed_base(worker_db)
    tg = _FailingTelegram(TelegramDeliveryError("500 Internal", status_code=500))
    worker = OutboxDeliveryWorker(telegram_client=tg)
    row = _tg_row()
    worker_db.add(row)
    worker_db.commit()

    # Попытка 1: ретрай.
    asyncio.run(worker.process_pending_messages(now=_NOW))
    worker_db.expire_all()
    assert worker_db.get(OutboxMessage, row.id).status == "pending"
    assert od._DELIVERY_ATTEMPTS[row.id] == 1

    # Попытка 2 = потолок: dead-letter.
    asyncio.run(worker.process_pending_messages(now=_NOW))
    worker_db.expire_all()
    row = worker_db.get(OutboxMessage, row.id)
    assert row.status == "failed"
    assert row.drop_reason == "delivery_retry_exhausted"
    assert row.claim_token is None
    assert row.id not in od._DELIVERY_ATTEMPTS
    assert len(alerts) == 1
    args, kwargs = alerts[0]
    assert args[0] == "P1"
    assert kwargs["dedupe_key"] == "outbox-retry-exhausted:telegram"


def test_outbox_attempt_counter_cleared_on_success(worker_db):
    """Успешная доставка после прошлых сбоев снимает счётчик попыток."""
    _seed_base(worker_db)
    worker = OutboxDeliveryWorker(telegram_client=_OkTelegram())
    row = _tg_row()
    worker_db.add(row)
    worker_db.commit()
    od._DELIVERY_ATTEMPTS[row.id] = 3  # были транзиентные сбои ранее

    asyncio.run(worker.process_pending_messages(now=_NOW))
    worker_db.expire_all()
    assert worker_db.get(OutboxMessage, row.id).status == "sent"
    assert row.id not in od._DELIVERY_ATTEMPTS


def test_outbox_max_permanent_error_dead_letters(worker_db):
    """MAX-канал, симметрично TG: перманентный 400 → failed сразу."""
    _seed_base(worker_db)
    max_client = _FailingMax(
        MaxDeliveryError("Bad Request: chat not found", status_code=400)
    )
    worker = OutboxDeliveryWorker(max_client=max_client)
    row = _tg_row(channel_type="max")
    worker_db.add(row)
    worker_db.commit()

    processed = asyncio.run(worker.process_pending_messages(now=_NOW))
    assert processed == 1

    worker_db.expire_all()
    row = worker_db.get(OutboxMessage, row.id)
    assert row.status == "failed"
    assert row.drop_reason == "delivery_permanent_400"
    assert row.id not in od._DELIVERY_ATTEMPTS
    assert max_client.calls == 1


def test_outbox_429_is_retryable_not_dead_letter(worker_db):
    """429 rate-limit — НЕ перманентная: ретрай со счётчиком, как транзиент."""
    _seed_base(worker_db)
    tg = _FailingTelegram(
        TelegramDeliveryError("Too Many Requests", status_code=429)
    )
    worker = OutboxDeliveryWorker(telegram_client=tg)
    row = _tg_row()
    worker_db.add(row)
    worker_db.commit()

    asyncio.run(worker.process_pending_messages(now=_NOW))
    worker_db.expire_all()
    row = worker_db.get(OutboxMessage, row.id)
    assert row.status == "pending"
    assert od._DELIVERY_ATTEMPTS[row.id] == 1


# ---------------------------------------------------------------------------
# #3 skill_platform_processor — rollback в except (класс #331)
# ---------------------------------------------------------------------------


def _stub_registry_with_failing_handler(garbage_id: str):
    from sreda.features.registry import FeatureRegistry
    from sreda.features.stub_skill import (
        STUB_SKILL_FEATURE_KEY,
        STUB_SKILL_NOOP_JOB_TYPE,
        StubSkillFeature,
    )

    async def _failing_handler(session, *, job, run_id, attempt_id):
        # Оставляем в транзакции «мусорную» строку и падаем: без rollback
        # recovery-commit утащил бы мусор в БД (и наоборот — rollback
        # обязан его откатить).
        session.add(
            Job(
                id=garbage_id,
                tenant_id="t1",
                workspace_id="w1",
                job_type="garbage_type",
                status="pending",
                payload_json="{}",
            )
        )
        session.flush()
        raise RuntimeError("handler boom")

    registry = FeatureRegistry()
    registry.register(StubSkillFeature())
    registry.register_skill_job_handler(
        feature_key=STUB_SKILL_FEATURE_KEY,
        job_type=STUB_SKILL_NOOP_JOB_TYPE,
        handler=_failing_handler,
    )
    return registry


def test_skill_processor_rolls_back_before_recovery(worker_db, monkeypatch):
    """Handler упал с грязной транзакцией → rollback ПЕРЕД recovery:
    мусор handler'а не коммитится, job/run/attempt помечаются failed
    (а не стрэндятся в 'running')."""
    from sreda.features.stub_skill import (
        STUB_SKILL_FEATURE_KEY,
        STUB_SKILL_NOOP_JOB_TYPE,
    )
    from sreda.workers.skill_platform_processor import SkillPlatformJobProcessor

    # Шпион за rollback: фиксируем сам факт вызова (эталон #331).
    real_rollback = _SA_Session.rollback
    rollback_calls: list = []

    def _spy_rollback(self):
        rollback_calls.append(1)
        return real_rollback(self)

    monkeypatch.setattr(_SA_Session, "rollback", _spy_rollback)

    worker_db.add(Tenant(id="t1", name="Tenant 1"))
    worker_db.add(Workspace(id="w1", tenant_id="t1", name="Workspace 1"))
    job_id = f"job_{uuid4().hex[:24]}"
    garbage_id = f"job_garbage_{uuid4().hex[:16]}"
    worker_db.add(
        Job(
            id=job_id,
            tenant_id="t1",
            workspace_id="w1",
            job_type=STUB_SKILL_NOOP_JOB_TYPE,
            status="pending",
            payload_json="{}",
        )
    )
    worker_db.commit()

    processor = SkillPlatformJobProcessor(
        _stub_registry_with_failing_handler(garbage_id)
    )
    processed = asyncio.run(processor.process_pending_jobs(limit=10))
    assert processed == 1

    worker_db.expire_all()
    # Мусор handler'а откачен вместе с его транзакцией.
    assert worker_db.get(Job, garbage_id) is None
    # Job НЕ остался 'running' — recovery доехал до failed.
    assert worker_db.get(Job, job_id).status == "failed"
    runs = worker_db.query(SkillRun).filter_by(
        feature_key=STUB_SKILL_FEATURE_KEY
    ).all()
    assert len(runs) == 1
    assert runs[0].status == "failed"
    attempts = worker_db.query(SkillRunAttempt).filter_by(run_id=runs[0].id).all()
    assert len(attempts) == 1
    assert attempts[0].status == "failed"
    assert attempts[0].error_class == "RuntimeError"
    # Сам rollback был вызван в except-пути.
    assert rollback_calls, "session.rollback() не вызван в recovery-пути"


# ---------------------------------------------------------------------------
# #4 job_runner — пер-воркерная изоляция в тике
# ---------------------------------------------------------------------------


def test_run_worker_isolated_returns_zero_on_failure_and_continues():
    async def _failing() -> int:
        raise RuntimeError("worker crash")

    async def _ok() -> int:
        return 7

    async def _main():
        bad = await _run_worker_isolated("bad_worker", _failing())
        good = await _run_worker_isolated("good_worker", _ok())
        return bad, good

    # Сбой первого воркера не мешает второму — сумма тика сохраняется.
    assert asyncio.run(_main()) == (0, 7)


def test_run_worker_isolated_does_not_swallow_cancelled_error():
    async def _cancelled() -> int:
        raise asyncio.CancelledError

    async def _main():
        await _run_worker_isolated("cancelled_worker", _cancelled())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_main())


def test_job_runner_tick_uses_isolation_for_every_worker():
    """Все 10 вызовов воркеров в process_pending_jobs_once обёрнуты в
    _run_worker_isolated (контракт фикса, защита от регресса при правках)."""
    import inspect

    src = inspect.getsource(jr.process_pending_jobs_once)
    assert src.count("_run_worker_isolated(") == 10


# ---------------------------------------------------------------------------
# #5 scheduler.py / sender.py — мёртвые стабы удалены
# ---------------------------------------------------------------------------


def test_dead_stub_modules_removed():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("sreda.workers.scheduler")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("sreda.workers.sender")


# ---------------------------------------------------------------------------
# #6 retention_cleanup — total включает plan_library_entries
# ---------------------------------------------------------------------------


def test_retention_total_includes_plan_library_entries():
    result = RetentionCleanupResult(jobs=3, plan_library_entries=4)
    assert result.total == 7
    assert RetentionCleanupResult(plan_library_entries=5).total == 5


# ---------------------------------------------------------------------------
# #7 housewife_reminder_worker — reminder без канала НЕ fired
# ---------------------------------------------------------------------------


def _seed_housewife(
    session, *, with_workspace: bool = True, with_channel: bool = True
) -> None:
    session.add(Tenant(id="tenant_1", name="Test"))
    if with_workspace:
        session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="D"))
    if with_channel:
        session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100"))
    else:
        session.add(User(id="user_1", tenant_id="tenant_1"))  # ни TG, ни MAX
    session.commit()


def test_reminder_without_channel_deferred_not_fired(worker_db):
    """Нет доставляемого канала → mark_fired НЕ вызывается: reminder
    остаётся pending, next_trigger_at отложен на NO_CHANNEL_RETRY_MINUTES,
    outbox-строк нет. Канал появится — доставим."""
    _seed_housewife(worker_db, with_channel=False)
    svc = HousewifeReminderService(worker_db)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    svc.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Без канала", trigger_at=now - timedelta(minutes=1),
    )
    worker_db.commit()

    fired = asyncio.run(HousewifeReminderWorker().process_pending(now=now))

    worker_db.expire_all()
    assert fired == 0
    assert worker_db.query(OutboxMessage).count() == 0
    reminder = worker_db.query(FamilyReminder).one()
    assert reminder.status == "pending"  # НЕ fired — напоминание не потеряно
    ntt = reminder.next_trigger_at
    if ntt.tzinfo is None:
        ntt = ntt.replace(tzinfo=UTC)
    expected = now + timedelta(minutes=NO_CHANNEL_RETRY_MINUTES)
    assert ntt == expected


def test_reminder_without_workspace_deferred_not_fired(worker_db):
    """Канал есть, но нет workspace → та же ветка: defer, не fired."""
    _seed_housewife(worker_db, with_workspace=False, with_channel=True)
    svc = HousewifeReminderService(worker_db)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    svc.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="Без workspace", trigger_at=now - timedelta(minutes=1),
    )
    worker_db.commit()

    fired = asyncio.run(HousewifeReminderWorker().process_pending(now=now))

    worker_db.expire_all()
    assert fired == 0
    assert worker_db.query(OutboxMessage).count() == 0
    reminder = worker_db.query(FamilyReminder).one()
    assert reminder.status == "pending"


def test_reminder_with_channel_still_fires(worker_db):
    """Контроль: с каналом поведение не изменилось — fired + outbox."""
    _seed_housewife(worker_db)
    svc = HousewifeReminderService(worker_db)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    svc.schedule(
        tenant_id="tenant_1", user_id="user_1",
        title="С каналом", trigger_at=now - timedelta(minutes=1),
    )
    worker_db.commit()

    fired = asyncio.run(HousewifeReminderWorker().process_pending(now=now))

    worker_db.expire_all()
    assert fired == 1
    assert worker_db.query(OutboxMessage).count() == 1
    reminder = worker_db.query(FamilyReminder).one()
    assert reminder.status == "fired"


# ---------------------------------------------------------------------------
# #8 proactive_events — sync-хендлер в asyncio.to_thread
# ---------------------------------------------------------------------------


def test_proactive_handler_runs_off_event_loop_thread(worker_db, monkeypatch):
    """Sync-хендлер выполняется НЕ в потоке event loop'а (to_thread):
    LLM-вызов в хендлере не заблокирует тик job_runner."""
    from sreda.db.models import Assistant, InboundEvent
    from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
    from sreda.db.repositories.inbound_event import (
        InboundEventDraft,
        InboundEventRepository,
    )
    from sreda.features.registry import FeatureRegistry
    from sreda.runtime.handlers import RuntimeReply
    from sreda.workers.proactive_events import ProactiveEventWorker

    feature_key = "audit_fix_stub"
    fresh = FeatureRegistry()
    monkeypatch.setattr(
        "sreda.features.app_registry.get_feature_registry", lambda: fresh
    )
    monkeypatch.setattr(
        "sreda.workers.proactive_events.get_feature_registry", lambda: fresh
    )

    worker_db.add(Tenant(id="t1", name="T"))
    worker_db.add(Workspace(id="w1", tenant_id="t1", name="W"))
    worker_db.flush()
    worker_db.add(Assistant(id="a1", tenant_id="t1", workspace_id="w1", name="S"))
    worker_db.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    plan = SubscriptionPlan(
        id=f"plan_{uuid4().hex[:16]}",
        plan_key=f"{feature_key}_basic_{uuid4().hex[:8]}",
        feature_key=feature_key,
        title="stub",
        description="",
        price_rub=0,
        credits_monthly_quota=1_000_000,
    )
    worker_db.add(plan)
    worker_db.flush()
    worker_db.add(
        TenantSubscription(
            id=f"sub_{uuid4().hex[:16]}",
            tenant_id="t1",
            plan_id=plan.id,
            status="active",
            starts_at=datetime.now(UTC) - timedelta(days=1),
            active_until=datetime.now(UTC) + timedelta(days=30),
        )
    )
    worker_db.commit()

    seen: dict = {}

    def handler(ctx):
        seen["thread_ident"] = threading.get_ident()
        return [RuntimeReply(text="ping", reply_markup=None, feature_key=feature_key)]

    fresh.register_proactive_handler(feature_key=feature_key, handler=handler)

    repo = InboundEventRepository(worker_db)
    event = repo.create_from_draft(
        InboundEventDraft(
            tenant_id="t1",
            user_id="u1",
            feature_key=feature_key,
            event_type="synthetic",
            external_event_key=f"evt-{uuid4().hex[:8]}",
            relevance_score=0.9,
            payload={"title": "x"},
        )
    )
    worker_db.commit()

    main_ident = threading.get_ident()
    processed = asyncio.run(ProactiveEventWorker().process_pending())

    worker_db.expire_all()
    assert processed == 1
    assert seen["thread_ident"] != main_ident  # выполнен в worker-потоке
    event = worker_db.get(InboundEvent, event.id)
    assert event.status == "consumed"
