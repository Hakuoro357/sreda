"""Регрессионные тесты на фиксы аудита 2026-07-18 (slug: runtime-executor).

Покрываемые находки:
  * runtime-core #1 [MAJOR] — action исполняется из ИСХОДНОГО envelope,
    а не из privacy-guard-sanitize'нутой копии (executor.py:143-146, 220);
  * runtime-core #2 [MAJOR] — reaper для застрявших ``running`` jobs
    (lease+settle-окно, executor.py reap_stale_running_jobs);
  * runtime-core #3 [MAJOR] — LRU-bound для InMemorySaver
    (graph.py _BoundedInMemorySaver);
  * runtime-core #4 [MINOR] — trace-meta первого outbox-row получает
    TG-id ПЕРВОГО reply, а не последнего (graph.py node_persist_replies);
  * runtime-core #5 [MINOR] — tolerant reader для AgentThread-дублей
    (executor.py _get_or_create_thread);
  * cross-concurrency Н3 [MINOR] — node_persist_error откатывает
    частичные flush'и handler'а перед error-commit (graph.py:541+).

Без сети и без Postgres: sqlite tmp-БД по образцу test_runtime_executor.py.
"""

import asyncio
import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models import (
    AgentRun,
    AgentThread,
    Assistant,
    Job,
    OutboxMessage,
    Tenant,
    User,
    Workspace,
)
from sreda.db.session import get_engine, get_session_factory
from sreda.runtime.dispatcher import ActionEnvelope
from sreda.runtime.executor import _REAPER_FIRST_SEEN, ActionRuntimeService
from sreda.runtime.graph import _BoundedInMemorySaver, node_persist_replies
from sreda.runtime.handlers import HANDLERS, ActionRuntimeError, RuntimeReply


class FakeTelegramClient:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []

    async def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        self.sent_messages.append({"chat_id": chat_id, "text": text})
        return {"ok": True}


class SeqTelegramClient:
    """Возвращает разные tg message_id/date на каждый send — для теста
    trace-атрибуции первого reply."""

    def __init__(self) -> None:
        self.calls = 0

    async def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        self.calls += 1
        return {
            "ok": True,
            "result": {"message_id": 100 + self.calls, "date": 1700000000 + self.calls},
        }


class NoEmbeddingClient:
    def embed_query(self, text: str) -> list[float]:
        raise RuntimeError("embeddings disabled in tests")


def _setup_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str):
    db_path = tmp_path / name
    key = base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    session.add(Tenant(id="tenant_1", name="Tenant 1"))
    session.add(Workspace(id="workspace_1", tenant_id="tenant_1", name="Workspace 1"))
    session.flush()
    session.add(
        Assistant(id="assistant_1", tenant_id="tenant_1", workspace_id="workspace_1", name="Sreda")
    )
    session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100000003"))
    session.commit()
    return session


def _teardown_db() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _make_action(*, action_type: str = "help.show", params: dict | None = None) -> ActionEnvelope:
    return ActionEnvelope(
        action_type=action_type,
        tenant_id="tenant_1",
        workspace_id="workspace_1",
        assistant_id="assistant_1",
        user_id="user_1",
        channel_type="telegram_dm",
        external_chat_id="100000003",
        bot_key="sreda",
        inbound_message_id=None,
        source_type="telegram_message",
        source_value="test",
        params=params or {},
    )


# ------------------------------------------------------------------
# runtime-core #1: исполнение исходного (не sanitize'нутого) envelope
# ------------------------------------------------------------------


def test_enqueue_persists_and_executes_raw_input(monkeypatch, tmp_path: Path) -> None:
    """Audit runtime-core #1: URL с query-string и «аллергия» должны доехать
    до handler'а (исполнение — по ИСХОДНОМУ envelope) БЕЗ privacy-плейсхолдеров.

    R1 C4: ПОСЛЕ завершения run'а input_json санитайзится at-rest (сырой
    payload больше не нужен для исполнения) — здесь проверяем оба инварианта:
    handler видел сырой текст, а осевший input_json — уже редактированный."""
    session = _setup_db(monkeypatch, tmp_path, "raw_input.db")
    try:
        captured: dict = {}

        def _capture_handler(_session, action, _context):
            captured["text"] = action.params.get("text")
            return [RuntimeReply(text="ок", reply_markup=None)]

        monkeypatch.setitem(HANDLERS, "conversation.chat", _capture_handler)

        service = ActionRuntimeService(
            session,
            telegram_client=FakeTelegramClient(),
            embedding_client=NoEmbeddingClient(),
        )
        raw_text = (
            "прочитай https://site.ru/page?id=7 и запомни: "
            "у меня аллергия на орехи, счёт 40817 810 0 1234 5678901"
        )
        queued = service.enqueue_action(
            _make_action(action_type="conversation.chat", params={"text": raw_text})
        )
        result = asyncio.run(service.process_job(queued.job_id))

        run = session.query(AgentRun).filter(AgentRun.id == queued.run_id).one()
        persisted = json.loads(run.input_json or "{}")
    finally:
        session.close()
        _teardown_db()

    assert result == "completed"
    # Handler получил ИСХОДНЫЙ текст — никаких [url]/[allergy]/[account_number].
    assert captured["text"] == raw_text
    assert "[url]" not in captured["text"]
    assert "[allergy]" not in captured["text"]
    # R1 C4: осевший input_json ПОСЛЕ завершения run'а — редактированный:
    # секреты/URL/мед-данные заменены плейсхолдерами, обычный текст сохранён.
    persisted_text = persisted["params"]["text"]
    assert persisted_text != raw_text
    assert "[url]" in persisted_text
    assert "[allergy]" in persisted_text
    assert "[account_number]" in persisted_text
    assert "https://site.ru" not in persisted_text
    assert "аллергия" not in persisted_text
    # Обычный (не-PII) текст диалога сохранён — history остаётся читаемым.
    assert "прочитай" in persisted_text and "запомни" in persisted_text


# ------------------------------------------------------------------
# runtime-core #2: reaper для застрявших running jobs (settle-окно)
# ------------------------------------------------------------------


def test_reaper_settle_window_reaps_stale_running_job(monkeypatch, tmp_path: Path) -> None:
    session = _setup_db(monkeypatch, tmp_path, "reaper.db")
    _REAPER_FIRST_SEEN.clear()
    try:
        service = ActionRuntimeService(session, telegram_client=FakeTelegramClient())
        stale = service.enqueue_action(_make_action())
        fresh = service.enqueue_action(_make_action())

        now = datetime.now(UTC)
        stale_job = session.get(Job, stale.job_id)
        stale_run = session.get(AgentRun, stale.run_id)
        stale_job.status = "running"
        stale_run.status = "running"
        stale_run.started_at = now - timedelta(seconds=3600)
        fresh_job = session.get(Job, fresh.job_id)
        fresh_run = session.get(AgentRun, fresh.run_id)
        fresh_job.status = "running"
        fresh_run.status = "running"
        fresh_run.started_at = now  # живой, только что заclaim'лен
        session.commit()

        # Первый sweep: марка + settle-окно, НЕ терминализация (Н2).
        reaped_first = asyncio.run(
            service.reap_stale_running_jobs(stale_after_seconds=10.0, settle_seconds=5.0)
        )
        assert reaped_first == 0
        session.expire_all()
        assert session.get(Job, stale.job_id).status == "running"

        # Второй sweep после settle-окна: CAS-финализация. ``now``
        # инжектим со сдвигом, чтобы окно гарантированно закрылось.
        reaped_second = asyncio.run(
            service.reap_stale_running_jobs(
                stale_after_seconds=10.0,
                settle_seconds=5.0,
                now=datetime.now(UTC) + timedelta(seconds=10),
            )
        )
        assert reaped_second == 1
        session.expire_all()
        assert session.get(Job, stale.job_id).status == "failed"
        reaped_run = session.get(AgentRun, stale.run_id)
        assert reaped_run.status == "failed"
        assert reaped_run.error_code == "runtime_stale_reaped"
        assert reaped_run.finished_at is not None
        # Свежий running job не тронут ни на одном sweep'е.
        assert session.get(Job, fresh.job_id).status == "running"
        assert session.get(AgentRun, fresh.run_id).status == "running"

        # Третий sweep: уже failed → no-op (идемпотентная финализация).
        reaped_third = asyncio.run(
            service.reap_stale_running_jobs(stale_after_seconds=10.0, settle_seconds=5.0)
        )
        assert reaped_third == 0
    finally:
        _REAPER_FIRST_SEEN.clear()
        session.close()
        _teardown_db()


# ------------------------------------------------------------------
# runtime-core #5: tolerant reader для AgentThread-дублей
# ------------------------------------------------------------------


def test_get_or_create_thread_tolerates_duplicate(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """DB-констрейнт уже добавлен db-uniques (миграция 20260718_0085) —
    реальный дубль через ORM не создать. Tolerant reader — второй слой
    FC-2 для БД, мигрированных с УЖЕ существующими дублями; симулируем
    выборку из двух строк стабом query-цепочки."""
    from unittest.mock import MagicMock

    now = datetime.now(UTC)
    oldest = AgentThread(
        id="thread_oldest_00000000001",
        tenant_id="tenant_1",
        workspace_id="workspace_1",
        assistant_id="assistant_1",
        channel_type="telegram_dm",
        external_chat_id="100000003",
        status="active",
        created_at=now,
        updated_at=now,
    )
    dup = AgentThread(
        id="thread_dup_000000000000001",
        tenant_id="tenant_1",
        workspace_id="workspace_1",
        assistant_id="assistant_1",
        channel_type="telegram_dm",
        external_chat_id="100000003",
        status="active",
        created_at=now + timedelta(hours=1),
        updated_at=now + timedelta(hours=1),
    )
    fake_query = MagicMock()
    fake_query.filter.return_value = fake_query
    fake_query.order_by.return_value = fake_query
    fake_query.limit.return_value = fake_query
    fake_query.all.return_value = [oldest, dup]
    session = MagicMock()
    session.query.return_value = fake_query

    service = ActionRuntimeService(session, telegram_client=None)
    with caplog.at_level(logging.ERROR, logger="sreda.runtime.executor"):
        # Раньше здесь падал MultipleResultsFound на каждом enqueue.
        thread = service._get_or_create_thread(_make_action())

    assert thread.id == "thread_oldest_00000000001"
    assert "duplicate AgentThread" in caplog.text


# ------------------------------------------------------------------
# runtime-core #3: LRU-bound для InMemorySaver
# ------------------------------------------------------------------


def _saver_config(thread_id: str, checkpoint_id: str = "c1") -> dict:
    return {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
            "checkpoint_id": checkpoint_id,
        }
    }


def _checkpoint(checkpoint_id: str) -> dict:
    return {
        "v": 1,
        "id": checkpoint_id,
        "ts": "2026-07-18T00:00:00+00:00",
        "channel_values": {"ch": f"val-{checkpoint_id}"},
        "channel_versions": {},
        "versions_seen": {},
        "pending_sends": [],
    }


def test_bounded_memory_saver_evicts_oldest_thread() -> None:
    saver = _BoundedInMemorySaver(max_threads=3)
    for i in range(5):
        tid = f"thread_{i}"
        saver.put(
            _saver_config(tid),
            _checkpoint(f"c{i}"),
            {"source": "input", "step": -1},
            {"ch": 1},
        )
        saver.put_writes(_saver_config(tid), [("ch", "w")], task_id=f"task_{i}")

    # Потолок 3: два самых старых thread'а evict'нуты из ВСЕХ store'ов.
    assert set(saver.storage.keys()) == {"thread_2", "thread_3", "thread_4"}
    assert {k[0] for k in saver.writes} == {"thread_2", "thread_3", "thread_4"}
    assert {k[0] for k in saver.blobs} == {"thread_2", "thread_3", "thread_4"}
    # Eviction не ломает чтение выживших thread'ов (config без
    # checkpoint_id → последний чекпоинт thread'а).
    tuple_ = saver.get_tuple(
        {"configurable": {"thread_id": "thread_4", "checkpoint_ns": ""}}
    )
    assert tuple_ is not None
    assert tuple_.checkpoint["id"] == "c4"

    # Touch существующего thread'а делает его самым свежим — следующий
    # eviction заберёт thread_3 (теперь самый старый), а не thread_2.
    saver.put(_saver_config("thread_2"), _checkpoint("c2b"), {"source": "input"}, {"ch": 2})
    saver.put(_saver_config("thread_5"), _checkpoint("c5"), {"source": "input"}, {"ch": 1})
    assert set(saver.storage.keys()) == {"thread_2", "thread_4", "thread_5"}
    # thread_5 не делал put_writes — владельцы writes: thread_2, thread_4.
    assert {k[0] for k in saver.writes} == {"thread_2", "thread_4"}
    assert {k[0] for k in saver.blobs} == {"thread_2", "thread_4", "thread_5"}


# ------------------------------------------------------------------
# runtime-core #4: trace-meta первого outbox-row
# ------------------------------------------------------------------


def test_trace_meta_attributes_first_reply_tg_ids(monkeypatch, tmp_path: Path) -> None:
    session = _setup_db(monkeypatch, tmp_path, "trace_first.db")
    emitted: list[dict] = []
    try:
        import sreda.services.trace as trace_mod

        monkeypatch.setattr(
            trace_mod, "emit_block", lambda ctx, **kwargs: emitted.append(kwargs)
        )

        action = _make_action()
        service = ActionRuntimeService(session, telegram_client=SeqTelegramClient())
        queued = service.enqueue_action(action)

        state = {
            "action": action.as_dict(),
            "run_id": queued.run_id,
            "job_id": queued.job_id,
            "context": {
                "tenant_id": "tenant_1",
                "workspace_id": "workspace_1",
                "assistant_id": "assistant_1",
            },
            "profile": {},
            "skill_configs": [],
            "replies": [
                {"text": "первый", "reply_markup": None, "feature_key": None},
                {"text": "второй", "reply_markup": None, "feature_key": None},
            ],
        }
        config = {
            "configurable": {
                "session": session,
                "telegram_client": SeqTelegramClient(),
                "bot_registry": None,
                "llm_client": None,
                "embedding_client": None,
                "ack_progress_controller": None,
            }
        }

        async def _run() -> dict:
            trace_mod.start_trace(channel="telegram")
            return await node_persist_replies(state, config)

        outcome = asyncio.run(_run())
        outbox_rows = session.query(OutboxMessage).order_by(OutboxMessage.id.asc()).all()
    finally:
        session.close()
        _teardown_db()

    assert outcome.get("outcome") == "completed"
    assert len(outbox_rows) == 2
    assert emitted, "inline-sent первый row должен эмитить trace"
    final_meta = emitted[0]["final_meta"]
    # message_id/date ПЕРВОГО reply (101/...001), не последнего (102/...002).
    assert final_meta["tg_message_id"] == 101
    assert final_meta["tg_date"] == 1700000001


# ------------------------------------------------------------------
# cross-concurrency Н3: node_persist_error откатывает частичный flush
# ------------------------------------------------------------------


def test_persist_error_rolls_back_partial_handler_flush(monkeypatch, tmp_path: Path) -> None:
    session = _setup_db(monkeypatch, tmp_path, "partial_flush.db")
    try:

        def _flush_then_raise(_session, _action, _context):
            junk = OutboxMessage(
                id="out_junk_partial_1",
                tenant_id="tenant_1",
                workspace_id="workspace_1",
                channel_type="telegram",
                status="pending",
                payload_json=json.dumps({"chat_id": "1", "text": "грязь"}),
                bot_key="sreda",
            )
            _session.add(junk)
            _session.flush()  # частичная мутация ДО raise
            raise ActionRuntimeError("runtime_unexpected_error", "бум")

        monkeypatch.setitem(HANDLERS, "help.show", _flush_then_raise)

        service = ActionRuntimeService(session, telegram_client=FakeTelegramClient())
        queued = service.enqueue_action(_make_action())
        result = asyncio.run(service.process_job(queued.job_id))

        session.expire_all()
        junk_count = (
            session.query(OutboxMessage)
            .filter(OutboxMessage.id == "out_junk_partial_1")
            .count()
        )
        run = session.get(AgentRun, queued.run_id)
        error_rows = (
            session.query(OutboxMessage)
            .filter(OutboxMessage.id != "out_junk_partial_1")
            .all()
        )
    finally:
        session.close()
        _teardown_db()

    assert result == "failed"
    assert run.status == "failed"
    # Частичный flush handler'а НЕ доехал до БД — rollback в
    # node_persist_error отбросил его до error-commit'а.
    assert junk_count == 0
    # А сам error-reply персистится как раньше.
    assert len(error_rows) == 1
    assert "бум" in json.loads(error_rows[0].payload_json)["text"]
