"""R1-фиксы аудита 2026-07-18, область W2 (runtime executor concurrency/PII).

Покрывает находки decision-log R1:

- C4  — reaper чистит сырой input_json (PII) на терминализации.
- M3  — reaper требует rowcount==1 от ОБОИХ условных апдейтов (Job+AgentRun):
        run уже терминализирован живым владельцем → откат, без рассинхрона.
- M4  — create-race первого enqueue: конкурентный INSERT thread'а выиграл
        UNIQUE → savepoint + re-query победителя вместо потери action'а.

(C4 completed-path проверен в test_audit_fix_runtime_executor; C3/M5-M8 — в
своих файлах.) Без сети/Postgres: sqlite tmp-БД.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sreda.config.settings import get_settings
from sreda.db.base import Base
from sreda.db.models import (
    AgentRun,
    AgentThread,
    Assistant,
    Job,
    Tenant,
    User,
    Workspace,
)
from sreda.db.session import get_engine, get_session_factory
from sreda.runtime.dispatcher import ActionEnvelope
from sreda.runtime.executor import ActionRuntimeService

_AGENT_EXECUTE_ACTION_JOB = "agent.execute_action"


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
    session.add(Assistant(
        id="assistant_1", tenant_id="tenant_1", workspace_id="workspace_1", name="Sreda",
    ))
    session.add(User(id="user_1", tenant_id="tenant_1", telegram_account_id="100000003"))
    session.commit()
    return session


def _teardown_db() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _make_action(*, params: dict | None = None) -> ActionEnvelope:
    return ActionEnvelope(
        action_type="conversation.chat",
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


def _seed_job_and_run(session, *, run_status: str, input_json: str) -> tuple[str, str]:
    thread = AgentThread(
        id="thread_x", tenant_id="tenant_1", workspace_id="workspace_1",
        assistant_id="assistant_1", channel_type="telegram_dm",
        external_chat_id="100000003", status="active",
    )
    session.add(thread)
    job = Job(
        id="job_x", tenant_id="tenant_1", workspace_id="workspace_1",
        job_type=_AGENT_EXECUTE_ACTION_JOB, status="running", payload_json="{}",
    )
    session.add(job)
    # flush thread+job до run: job_id — nullable FK, UoW не гарантирует
    # порядок вставки jobs перед agent_runs без явного flush.
    session.flush()
    run = AgentRun(
        id="run_x", thread_id="thread_x", tenant_id="tenant_1",
        workspace_id="workspace_1", assistant_id="assistant_1", job_id="job_x",
        action_type="conversation.chat", status=run_status,
        started_at=datetime.now(UTC), input_json=input_json,
    )
    session.add(run)
    session.commit()
    return job.id, run.id


# ---------------------------------------------------------------------------
# M3 — reaper требует rowcount==1 и от AgentRun-апдейта
# ---------------------------------------------------------------------------


def test_m3_reaper_rolls_back_when_run_already_completed(monkeypatch, tmp_path) -> None:
    """Гонка: между reaper-запросом (видел run='running') и условным апдейтом
    живой владелец завершил run ('completed'). Job-апдейт прошёл бы, а
    AgentRun-апдейт — 0 строк. Без M3 закоммитили бы Job=failed поверх
    run=completed. Фикс: rowcount!=1 у ЛЮБОГО апдейта → полный откат."""
    session = _setup_db(monkeypatch, tmp_path, "m3.db")
    try:
        job_id, run_id = _seed_job_and_run(
            session, run_status="completed", input_json='{"params": {"text": "x"}}',
        )
        service = ActionRuntimeService(session)
        reaped = service._reap_stale_job(
            job_id=job_id, run_id=run_id, now=datetime.now(UTC),
        )
        assert reaped == 0
        session.expire_all()
        # Ни Job=failed, ни рассинхрон: Job вернулся в running, run остался completed.
        assert session.get(Job, job_id).status == "running"
        assert session.get(AgentRun, run_id).status == "completed"
    finally:
        session.close()
        _teardown_db()


# ---------------------------------------------------------------------------
# C4 — reaper чистит сырой input_json (PII) на терминализации
# ---------------------------------------------------------------------------


def test_c4_reaper_clears_input_json(monkeypatch, tmp_path) -> None:
    session = _setup_db(monkeypatch, tmp_path, "c4reap.db")
    try:
        secret = '{"params": {"text": "пароль qwerty123 ссылка https://x.ru/apikey/abc123def"}}'
        job_id, run_id = _seed_job_and_run(
            session, run_status="running", input_json=secret,
        )
        service = ActionRuntimeService(session)
        reaped = service._reap_stale_job(
            job_id=job_id, run_id=run_id, now=datetime.now(UTC),
        )
        assert reaped == 1
        session.expire_all()
        run = session.get(AgentRun, run_id)
        assert run.status == "failed"
        # Сырой payload с секретом стёрт.
        assert run.input_json == "{}"
        assert "qwerty123" not in (run.input_json or "")
    finally:
        session.close()
        _teardown_db()


# ---------------------------------------------------------------------------
# M4 — create-race первого enqueue: savepoint + re-query победителя
# ---------------------------------------------------------------------------


class _EmptyQuery:
    """Псевдо-query, возвращающий пустой список для ПЕРВОГО listing-запроса
    thread'ов (симулирует окно гонки, где ни один enqueue не видит чужой)."""

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def all(self):
        return []


def test_m4_create_race_returns_winner(monkeypatch, tmp_path) -> None:
    """Конкурентный enqueue уже создал thread (t, ch, chat); наш initial
    listing его «не видит» (окно гонки) → идём в create → flush падает на
    uq_agent_threads_tenant_channel_chat. Фикс: savepoint изолирует конфликт,
    re-query возвращает победителя, action не теряется."""
    session = _setup_db(monkeypatch, tmp_path, "m4.db")
    try:
        action = _make_action(params={"text": "привет"})
        # Победитель уже в БД (закоммичен «конкурентным» enqueue).
        winner = AgentThread(
            id="thread_winner", tenant_id="tenant_1", workspace_id="workspace_1",
            assistant_id="assistant_1", channel_type=action.channel_type,
            external_chat_id=action.external_chat_id, status="active",
        )
        session.add(winner)
        session.commit()

        service = ActionRuntimeService(session)

        # Первый listing thread'ов вернёт [] (гонка), последующие (recovery
        # re-query) — реальные.
        real_query = session.query
        state = {"first": True}

        def patched_query(model, *a, **k):
            if model is AgentThread and state["first"]:
                state["first"] = False
                return _EmptyQuery()
            return real_query(model, *a, **k)

        monkeypatch.setattr(session, "query", patched_query)

        thread = service._get_or_create_thread(action)
        assert thread.id == "thread_winner"  # вернулся победитель, не дубль
        # Дубля в БД не появилось.
        monkeypatch.undo()
        count = session.query(AgentThread).filter(
            AgentThread.tenant_id == "tenant_1",
            AgentThread.channel_type == action.channel_type,
            AgentThread.external_chat_id == action.external_chat_id,
        ).count()
        assert count == 1
    finally:
        session.close()
        _teardown_db()
