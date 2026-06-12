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
