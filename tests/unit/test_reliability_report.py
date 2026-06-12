"""#139 — мерило надёжности: ежедневный автоотчёт (этап 0 программы).

Чек-лист приёмки #139: формат сводки (п.1, test_reliability_report_format),
не чаще раза в сутки + откат после провала (п.2,
test_reliability_report_backoff), классификация по фиксированным
наблюдаемым признакам (п.3, test_failure_classification).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import InboundMessage, OutboxMessage, Tenant, Workspace
from sreda.db.models.runtime import AgentRun, AgentThread
from sreda.workers import reliability_report as rr_module
from sreda.workers.reliability_report import (
    ReliabilityReportWorker,
    format_report,
    gather_day_counts,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="T"))
    sess.add(Workspace(id="w1", tenant_id="t1", name="W"))
    sess.add(AgentThread(id="th1", tenant_id="t1", workspace_id="w1",
                         channel_type="telegram", external_chat_id="42"))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


NOW = datetime(2026, 6, 12, 4, 0, tzinfo=timezone.utc)


def _run(sess, rid, status, hours_ago):
    sess.add(AgentRun(id=rid, thread_id="th1", tenant_id="t1",
                      workspace_id="w1", action_type="chat", status=status,
                      created_at=NOW - timedelta(hours=hours_ago)))


def _inbound(sess, mid, processing_status, hours_ago):
    sess.add(InboundMessage(
        id=mid, tenant_id="t1", workspace_id="w1",
        channel_type="telegram", channel_account_id="42", bot_key="sreda",
        external_update_id=f"u_{mid}", status="processed",
        processing_status=processing_status,
        created_at=NOW - timedelta(hours=hours_ago),
    ))


def test_failure_classification(session, tmp_path: Path) -> None:
    """Классы — по наблюдаемым статусам, не эвристика по тексту."""
    # окно: сутки до NOW
    _run(session, "r1", "completed", 2)
    _run(session, "r2", "failed", 3)
    _run(session, "r3", "completed", 30)  # вне окна
    _inbound(session, "i1", "processed", 2)
    _inbound(session, "i2", "processing_started", 5)  # застрял
    _inbound(session, "i3", "ingested", 0.01)  # свежий — НЕ застрял
    session.add(OutboxMessage(
        id="o1", tenant_id="t1", workspace_id="w1",
        channel_type="telegram", payload_json="{}", status="failed",
        created_at=NOW - timedelta(hours=1),
    ))
    session.commit()

    log = tmp_path / "job-runner.log"
    log.write_text(
        "2026-06-10 09:00:00 ERROR sreda.composer ПОЛОМКА показана пользователю: x\n"
        "2026-06-12 03:00:00 ERROR sreda.composer ПОЛОМКА показана пользователю: y\n",
        encoding="utf-8",
    )
    c = gather_day_counts(session, now=NOW, log_path=str(log))
    assert c.turns_total == 2          # r1+r2 (r3 вне окна)
    assert c.runs_failed == 1          # r2
    assert c.inbound_stuck == 1        # i2 (i3 свежий)
    assert c.outbox_failed == 1
    assert c.breakdowns_shown == 1     # только строка в окне (12-е)


def test_reliability_report_format(tmp_path: Path) -> None:
    counts = SimpleNamespace(
        turns_total=40, runs_failed=1, inbound_stuck=1,
        outbox_failed=0, breakdowns_shown=2,
    )
    history = [
        {"date": "2026-06-11", "total": 50, "failures": 2},
        {"date": "2026-06-12", "total": 40, "failures": 4},
    ]
    text = format_report(counts, history, kpi_threshold_pct=95.0)
    assert "ходов: 40" in text
    assert "исполнение: 1" in text
    assert "застряло: 1" in text
    assert "доставка: 0" in text
    assert "поломок показано: 2" in text
    # KPI: 90 ходов, 6 провалов → 93.3% — ниже порога, маркер тревоги
    assert "93.3%" in text
    assert "ниже порога 95" in text


@pytest.mark.asyncio
async def test_reliability_report_backoff(tmp_path: Path, monkeypatch) -> None:
    """Не чаще раза в сутки; провал НЕ повторяется каждый тик (#127-урок)."""
    sent: list = []
    monkeypatch.setattr(rr_module, "send_admin_alert",
                        lambda *a, **kw: sent.append(a))
    from sreda.workers.reliability_report import DayCounts
    ok = MagicMock(return_value=DayCounts(1, 0, 0, 0, 0))
    monkeypatch.setattr(rr_module, "gather_day_counts", ok)
    state = tmp_path / "st.json"
    w = ReliabilityReportWorker(MagicMock(), state_file=str(state))
    assert await w.process_pending() == 1
    assert len(sent) == 1
    # второй тик тех же суток — отчёт НЕ шлётся
    assert await w.process_pending() == 0
    assert len(sent) == 1
    data = json.loads(state.read_text(encoding="utf-8"))
    assert data.get("history"), "история для KPI обязана копиться"

    # провал сбора → откат, не шторм
    boom = MagicMock(side_effect=RuntimeError("db down"))
    monkeypatch.setattr(rr_module, "gather_day_counts", boom)
    state2 = tmp_path / "st2.json"
    w2 = ReliabilityReportWorker(MagicMock(), state_file=str(state2))
    assert await w2.process_pending() == 0
    assert await w2.process_pending() == 0
    assert boom.call_count == 1, "повтор до отката — шторм"


def test_kpi_zero_turns_with_failures_is_alarm() -> None:
    """Codex R1 medium: сигналы без ходов ≠ «100% чистых»."""
    counts = SimpleNamespace(turns_total=0, runs_failed=0, inbound_stuck=2,
                             outbox_failed=0, breakdowns_shown=0)
    text = format_report(counts, [{"date": "2026-06-12", "total": 0,
                                   "failures": 2}])
    assert "0.0%" in text and "ниже порога" in text


def test_kpi_clamp_failures_above_total() -> None:
    """Субагент: классы пересекаются — провалов может быть больше ходов."""
    counts = SimpleNamespace(turns_total=1, runs_failed=1, inbound_stuck=1,
                             outbox_failed=1, breakdowns_shown=1)
    text = format_report(counts, [{"date": "2026-06-12", "total": 1,
                                   "failures": 4}])
    assert "0.0%" in text  # clamp: не уходит в минус


def test_history_dedupe_and_calendar_window(tmp_path: Path, monkeypatch) -> None:
    """Субагент (мутация показала дыру): повтор той же даты не задваивает
    день; окно — календарные 14 дней, не «последние 14 записей»."""
    sent: list = []
    monkeypatch.setattr(rr_module, "send_admin_alert",
                        lambda *a, **kw: sent.append(a))
    from sreda.workers.reliability_report import DayCounts
    monkeypatch.setattr(rr_module, "gather_day_counts",
                        MagicMock(return_value=DayCounts(5, 0, 0, 0, 0)))
    monkeypatch.setattr(rr_module, "count_breakdown_lines",
                        MagicMock(return_value=0))
    state = tmp_path / "st.json"
    today = datetime.now(timezone.utc).date()
    old = (today - timedelta(days=30)).isoformat()
    state.write_text(json.dumps({
        "history": [
            {"date": old, "total": 9, "failures": 9},          # старше окна
            {"date": today.isoformat(), "total": 1, "failures": 1},  # дубль дня
        ],
    }), encoding="utf-8")
    w = ReliabilityReportWorker(MagicMock(), state_file=str(state))
    import asyncio as _a
    assert _a.get_event_loop_policy() is not None
    assert _a.run(w.process_pending()) == 1
    data = json.loads(state.read_text(encoding="utf-8"))
    dates = [h["date"] for h in data["history"]]
    assert dates.count(today.isoformat()) == 1, "дубль дня в истории"
    assert old not in dates, "запись старше 14 календарных дней не вычищена"
    assert data["history"][-1]["total"] == 5  # свежий прогон победил


def test_breakdown_glob_scans_all_logs(tmp_path: Path) -> None:
    """Субагент CRITICAL: поломки пишут РАЗНЫЕ процессы — glob по всем."""
    from sreda.workers.reliability_report import count_breakdown_lines
    (tmp_path / "telegram-poller.log").write_text(
        "2026-06-12 06:00:00 ERROR x ПОЛОМКА показана пользователю: a\n",
        encoding="utf-8")
    (tmp_path / "job-runner.log").write_text(
        "2026-06-12 06:30:00 ERROR x ПОЛОМКА показана пользователю: b\n"
        "мусор без даты ПОЛОМКА показана пользователю: не считаем\n",
        encoding="utf-8")
    n = count_breakdown_lines(
        str(tmp_path / "*.log"),
        since=datetime(2026, 6, 12, 0, 0, tzinfo=timezone.utc),
        until=datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc))
    assert n == 2  # обе из двух логов; строка без даты НЕ учтена


def test_breakdown_msk_timestamps_converted(tmp_path: Path) -> None:
    """Субагент MAJOR: лог в MSK — 23:30 MSK 11-го = 20:30 UTC 11-го,
    в окно «12-е UTC» не попадает; 02:00 MSK 12-го = 23:00 UTC 11-го."""
    from sreda.workers.reliability_report import count_breakdown_lines
    (tmp_path / "a.log").write_text(
        "2026-06-12 02:00:00 ERROR x ПОЛОМКА показана пользователю: y\n",
        encoding="utf-8")
    in_11th_utc = count_breakdown_lines(
        str(tmp_path / "*.log"),
        since=datetime(2026, 6, 11, 0, 0, tzinfo=timezone.utc),
        until=datetime(2026, 6, 12, 0, 0, tzinfo=timezone.utc))
    assert in_11th_utc == 1, "02:00 MSK 12-го — это ещё 11-е UTC"


@pytest.mark.asyncio
async def test_unwritable_state_falls_back_and_backs_off(
    tmp_path: Path, monkeypatch,
) -> None:
    """Codex R2 (оба): основной state-путь недоступен → запасной /tmp
    держит откат и счётчик серии (нет повторных отправок)."""
    sent: list = []
    monkeypatch.setattr(rr_module, "send_admin_alert",
                        lambda *a, **kw: sent.append(a))
    from sreda.workers.reliability_report import DayCounts
    monkeypatch.setattr(rr_module, "gather_day_counts",
                        MagicMock(return_value=DayCounts(1, 0, 0, 0, 0)))
    monkeypatch.setattr(rr_module, "count_breakdown_lines",
                        MagicMock(return_value=0))
    w = ReliabilityReportWorker(MagicMock(),
                                state_file=str(tmp_path / "nodir" / "st.json"))
    # основной путь сломан: каталог-файл
    (tmp_path / "nodir").write_text("файл вместо каталога", encoding="utf-8")
    w.fallback_state_file = tmp_path / "fb.json"
    assert await w.process_pending() in (0, 1)
    first_sent = len(sent)
    # второй тик: состояние читается из запасного → НЕ повторная отправка
    assert await w.process_pending() == 0
    assert len(sent) == first_sent, "повторная отправка при сломанном state"
    assert w.fallback_state_file.exists()


def test_breakdown_scans_rotated_log(tmp_path: Path) -> None:
    """Codex R2 high: маркеры из .log.1 (ротация внутри окна) учитываются."""
    from sreda.workers.reliability_report import count_breakdown_lines
    (tmp_path / "a.log").write_text(
        "2026-06-12 06:00:00 ERROR x ПОЛОМКА показана пользователю: a\n",
        encoding="utf-8")
    (tmp_path / "a.log.1").write_text(
        "2026-06-12 05:00:00 ERROR x ПОЛОМКА показана пользователю: b\n",
        encoding="utf-8")
    n = count_breakdown_lines(
        str(tmp_path / "*.log"),
        since=datetime(2026, 6, 12, 0, 0, tzinfo=timezone.utc),
        until=datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc))
    assert n == 2


def test_stale_readable_primary_does_not_shadow_fallback(tmp_path: Path) -> None:
    """Codex R3 high: основной читаем, но устарел (записи шли в запасной)
    → чтение берёт свежайший state, повторной отправки нет."""
    w = ReliabilityReportWorker(MagicMock(),
                                state_file=str(tmp_path / "st.json"))
    w.fallback_state_file = tmp_path / "fb.json"
    today = datetime.now(timezone.utc)
    (tmp_path / "st.json").write_text(json.dumps(
        {"last_run_at": (today - timedelta(days=3)).isoformat()}),
        encoding="utf-8")
    w.fallback_state_file.write_text(json.dumps(
        {"last_run_at": today.isoformat()}), encoding="utf-8")
    state = w._read_state()
    assert state["last_run_at"] == today.isoformat()
    assert not w._should_run(today, state), "сегодня уже отправлено (fallback)"
