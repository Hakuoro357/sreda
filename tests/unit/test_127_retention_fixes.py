"""#127 — ретеншн: FK-взрыв inbound_messages + шторм повторов раз в секунду.

Прод 2026-06-09..11: cleanup падал на agent_runs_inbound_message_id_fkey
(~264k ошибок/сутки), чистка не двигалась трое суток. Два корня:
1. inbound_messages (окно 30 дней) удалялись вслепую, а agent_runs живут
   90 дней и держат FK → входящее 30–90 дней с живым прогоном = взрыв.
   Фикс: обнулить ссылки ПЕРЕД удалением (окно 30 дней соблюдаем строго —
   это персональные данные; agent_run переживает без ссылки, колонка
   nullable).
2. Состояние воркера писалось только при успехе → провал повторялся на
   каждом тике джоб-раннера. Фикс: откат-пауза после провала + P1-алерт
   после 3 подряд (со сбросом при успехе).

Чек-лист принятия #127 называет эти тесты.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import InboundMessage, Job, Tenant, User, Workspace
from sreda.db.models.runtime import AgentRun, AgentThread
from sreda.maintenance.retention_cleanup import cleanup_runtime_retention
from sreda.workers import retention_worker as rw_module
from sreda.workers.retention_worker import RetentionWorker


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    # Codex R1 high MINOR: без PRAGMA SQLite НЕ проверяет FK — тест
    # «переживает ссылки» был бы бутафорией.
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="T"))
    sess.add(Workspace(id="w1", tenant_id="t1", name="W"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    sess.add(AgentThread(
        id="th1", tenant_id="t1", workspace_id="w1",
        channel_type="telegram", external_chat_id="42",
    ))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


def _inbound(sess, mid: str, age_days: int) -> None:
    sess.add(InboundMessage(
        id=mid, tenant_id="t1", workspace_id="w1", user_id="u1",
        channel_type="telegram", channel_account_id="42", bot_key="sreda",
        external_update_id=f"upd_{mid}", status="processed",
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    ))


def _run(
    sess, rid: str, inbound_id: str | None, age_days: int,
    job_id: str | None = None,
) -> None:
    sess.add(AgentRun(
        id=rid, thread_id="th1", tenant_id="t1", workspace_id="w1",
        action_type="chat", status="completed",
        inbound_message_id=inbound_id, job_id=job_id,
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    ))


def _job(sess, jid: str, age_days: int, status: str = "completed") -> None:
    sess.add(Job(
        id=jid, tenant_id="t1", workspace_id="w1", job_type="reminder",
        status=status,
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    ))


def test_referenced_old_inbound_unlinked_then_deleted(session) -> None:
    """Ровно прод-падение: входящее старше 30 дней, на него ссылается
    живой (моложе 90) agent_run → ссылка обнуляется, входящее удаляется,
    прогон выживает."""
    _inbound(session, "in_old_referenced", age_days=45)
    _run(session, "run_alive", "in_old_referenced", age_days=45)
    _inbound(session, "in_old_free", age_days=45)
    _inbound(session, "in_young", age_days=5)
    session.commit()

    result = cleanup_runtime_retention(session, now=datetime.now(timezone.utc))
    session.commit()

    assert session.get(InboundMessage, "in_old_referenced") is None
    assert session.get(InboundMessage, "in_old_free") is None
    assert session.get(InboundMessage, "in_young") is not None
    run = session.get(AgentRun, "run_alive")
    assert run is not None and run.inbound_message_id is None
    assert result.inbound_messages == 2
    assert result.agent_runs_unlinked == 1


def test_young_referenced_inbound_untouched(session) -> None:
    _inbound(session, "in_fresh", age_days=3)
    _run(session, "run_fresh", "in_fresh", age_days=3)
    session.commit()

    cleanup_runtime_retention(session, now=datetime.now(timezone.utc))
    session.commit()

    run = session.get(AgentRun, "run_fresh")
    assert session.get(InboundMessage, "in_fresh") is not None
    assert run.inbound_message_id == "in_fresh"  # ссылка цела


def test_referenced_old_job_unlinked_then_deleted(session) -> None:
    """Калибровочный субагент (зонд: 556/557 на проде): jobs (30д) держатся
    FK agent_runs.job_id (90д) — тот же класс, что inbound. Ссылка
    обнуляется, job удаляется, прогон выживает."""
    _job(session, "job_old_referenced", age_days=45)
    _run(session, "run_holds_job", None, age_days=45,
         job_id="job_old_referenced")
    _job(session, "job_old_free", age_days=45)
    _job(session, "job_old_pending", age_days=45, status="pending")
    _job(session, "job_young", age_days=5)
    session.commit()

    result = cleanup_runtime_retention(session, now=datetime.now(timezone.utc))
    session.commit()

    assert session.get(Job, "job_old_referenced") is None
    assert session.get(Job, "job_old_free") is None
    assert session.get(Job, "job_old_pending") is not None  # не терминальный
    assert session.get(Job, "job_young") is not None
    run = session.get(AgentRun, "run_holds_job")
    assert run is not None and run.job_id is None
    assert result.jobs == 2
    assert result.agent_runs_job_unlinked == 1


# ---------------------------------------------------------------------------
# Воркер: откат-пауза + алерт после серии провалов
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_backoff_prevents_storm(tmp_path: Path, monkeypatch) -> None:
    """Провал → немедленный повтор на следующем тике ЗАПРЕЩЁН (раньше —
    раз в секунду трое суток)."""
    boom = MagicMock(side_effect=RuntimeError("FK"))
    monkeypatch.setattr(rw_module, "cleanup_runtime_retention", boom)
    monkeypatch.setattr(rw_module, "send_admin_alert", lambda *a, **kw: None)

    w = RetentionWorker(MagicMock(), state_file=str(tmp_path / "st.json"))
    assert await w.process_pending() == 0
    assert boom.call_count == 1
    # следующий тик (через «секунду») — чистка НЕ зовётся
    assert await w.process_pending() == 0
    assert boom.call_count == 1, "повтор до истечения отката — шторм"


@pytest.mark.asyncio
async def test_alert_after_three_consecutive_failures(
    tmp_path: Path, monkeypatch,
) -> None:
    boom = MagicMock(side_effect=RuntimeError("FK"))
    monkeypatch.setattr(rw_module, "cleanup_runtime_retention", boom)
    alerts: list = []
    monkeypatch.setattr(
        rw_module, "send_admin_alert", lambda *a, **kw: alerts.append(a),
    )

    state = tmp_path / "st.json"
    w = RetentionWorker(MagicMock(), state_file=str(state))
    for i in range(3):
        # каждый заход — «после отката»: сдвигаем записанное время назад
        if state.exists():
            data = json.loads(state.read_text(encoding="utf-8"))
            data["last_failure_at"] = (
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).isoformat()
            state.write_text(json.dumps(data), encoding="utf-8")
        await w.process_pending()
    assert boom.call_count == 3
    assert alerts, "после 3 провалов подряд обязан уйти P1-алерт"


@pytest.mark.asyncio
async def test_success_resets_failure_streak(tmp_path: Path, monkeypatch) -> None:
    ok = MagicMock()
    ok.return_value.total = 1
    monkeypatch.setattr(rw_module, "cleanup_runtime_retention", ok)
    alerts: list = []
    monkeypatch.setattr(
        rw_module, "send_admin_alert", lambda *a, **kw: alerts.append(a),
    )
    state = tmp_path / "st.json"
    # имитация прежних двух провалов
    state.write_text(json.dumps({
        "last_failure_at": (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat(),
        "failure_count": 2,
    }), encoding="utf-8")

    w = RetentionWorker(MagicMock(), state_file=str(state))
    assert await w.process_pending() == 1
    data = json.loads(state.read_text(encoding="utf-8"))
    assert data.get("failure_count", 0) == 0
    assert not alerts


def test_inbound_cleanup_chunks_cover_backlog(session, monkeypatch) -> None:
    """Codex R1 medium: бэклог чистится порциями — несколько чанков
    обязаны добрать всё."""
    from sreda.maintenance import retention_cleanup as rc
    monkeypatch.setattr(rc, "INBOUND_CHUNK_SIZE", 2)

    for i in range(5):
        _inbound(session, f"in_bulk_{i}", age_days=40)
    _run(session, "run_bulk", "in_bulk_3", age_days=40)
    session.commit()

    result = cleanup_runtime_retention(session, now=datetime.now(timezone.utc))
    session.commit()
    assert result.inbound_messages == 5
    assert result.agent_runs_unlinked == 1
    assert session.query(InboundMessage).count() == 0


@pytest.mark.asyncio
async def test_success_commits_db_transaction(tmp_path: Path, monkeypatch) -> None:
    """Codex R1 CRITICAL: cleanup только flush'ит, job_runner закрывает
    сессию без commit — успех обязан коммититься ВОРКЕРОМ до записи state."""
    ok = MagicMock()
    ok.return_value.total = 3
    monkeypatch.setattr(rw_module, "cleanup_runtime_retention", ok)
    sess = MagicMock()
    w = RetentionWorker(sess, state_file=str(tmp_path / "st.json"))
    assert await w.process_pending() == 3
    sess.commit.assert_called_once()


@pytest.mark.asyncio
async def test_commit_failure_recorded_as_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    ok = MagicMock()
    ok.return_value.total = 3
    monkeypatch.setattr(rw_module, "cleanup_runtime_retention", ok)
    monkeypatch.setattr(rw_module, "send_admin_alert", lambda *a, **kw: None)
    sess = MagicMock()
    sess.commit.side_effect = RuntimeError("commit down")
    state = tmp_path / "st.json"
    w = RetentionWorker(sess, state_file=str(state))
    assert await w.process_pending() == 0
    sess.rollback.assert_called()
    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["failure_count"] == 1 and "last_run_at" not in data


@pytest.mark.asyncio
async def test_corrupt_failure_count_does_not_break_failure_path(
    tmp_path: Path, monkeypatch,
) -> None:
    """Codex R2 MINOR (оба): мусор в failure_count не должен ронять
    обработчик провала — счётчик сбрасывается на 1, запись проходит."""
    boom = MagicMock(side_effect=RuntimeError("FK"))
    monkeypatch.setattr(rw_module, "cleanup_runtime_retention", boom)
    monkeypatch.setattr(rw_module, "send_admin_alert", lambda *a, **kw: None)
    state = tmp_path / "st.json"
    state.write_text(json.dumps({"failure_count": "bad"}), encoding="utf-8")

    w = RetentionWorker(MagicMock(), state_file=str(state))
    assert await w.process_pending() == 0  # не подняло исключение
    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["failure_count"] == 1
    assert "last_failure_at" in data


@pytest.mark.asyncio
async def test_naive_timestamp_in_state_does_not_crash(
    tmp_path: Path, monkeypatch,
) -> None:
    """Субагент MINOR: наивный ISO-ts в state дал бы TypeError (aware−naive)
    ВНЕ try — лог-шторм без самолечения. Парсер считает наивный ts UTC."""
    ok = MagicMock()
    ok.return_value.total = 0
    monkeypatch.setattr(rw_module, "cleanup_runtime_retention", ok)
    state = tmp_path / "st.json"
    state.write_text(
        json.dumps({"last_run_at": "2026-06-11T10:00:00"}),  # без таймзоны
        encoding="utf-8",
    )
    w = RetentionWorker(MagicMock(), state_file=str(state))
    await w.process_pending()  # главное — не TypeError
