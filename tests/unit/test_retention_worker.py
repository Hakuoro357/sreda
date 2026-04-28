"""Tests for RetentionWorker — 24h-throttle scheduler around
``cleanup_runtime_retention``.

Стратегия: вместо реального запуска cleanup (который требует таблиц)
мокаем `cleanup_runtime_retention` через monkeypatch. Тесты проверяют
поведение throttle-логики и работу со state-файлом.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sreda.workers import retention_worker as rw_module
from sreda.workers.retention_worker import RetentionWorker


def _result_stub(total: int = 5) -> MagicMock:
    """Build a fake RetentionCleanupResult-like object."""
    m = MagicMock()
    m.total = total
    m.agent_runs = 0
    m.inbound_messages = total
    m.jobs = 0
    m.outbox_messages_sent = 0
    m.outbox_messages_failed = 0
    m.secure_records_eds_connect_payload = 0
    m.skill_ai_executions = 0
    m.skill_events_debug_info = 0
    m.skill_events_warn_error = 0
    m.skill_run_attempts = 0
    m.skill_runs = 0
    return m


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "retention-state.json"


@pytest.fixture
def cleanup_mock(monkeypatch):
    """Replace cleanup_runtime_retention with a mock that returns
    a configurable RetentionCleanupResult-like object."""
    mock = MagicMock(return_value=_result_stub(total=42))
    monkeypatch.setattr(rw_module, "cleanup_runtime_retention", mock)
    return mock


@pytest.mark.asyncio
async def test_first_call_runs_cleanup_and_writes_state(
    state_path: Path, cleanup_mock
):
    """No state file → run cleanup, persist last_run_at."""
    session = MagicMock()
    worker = RetentionWorker(session, state_file=str(state_path))

    deleted = await worker.process_pending()

    assert deleted == 42
    assert cleanup_mock.call_count == 1
    assert state_path.exists()
    data = json.loads(state_path.read_text(encoding="utf-8"))
    assert "last_run_at" in data
    # ISO format that fromisoformat can re-parse
    parsed = datetime.fromisoformat(data["last_run_at"])
    assert parsed.tzinfo is not None
    assert data["total_deleted"] == 42


@pytest.mark.asyncio
async def test_second_call_within_24h_skips(state_path: Path, cleanup_mock):
    """If last_run < 24h ago — skip cleanup (return 0)."""
    # Pre-populate state file with timestamp 1 hour ago
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    state_path.write_text(
        json.dumps({"last_run_at": one_hour_ago, "total_deleted": 0}),
        encoding="utf-8",
    )

    session = MagicMock()
    worker = RetentionWorker(session, state_file=str(state_path))
    deleted = await worker.process_pending()

    assert deleted == 0
    assert cleanup_mock.call_count == 0


@pytest.mark.asyncio
async def test_call_after_24h_runs_again(state_path: Path, cleanup_mock):
    """If last_run > 24h ago — run cleanup again."""
    too_long_ago = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    state_path.write_text(
        json.dumps({"last_run_at": too_long_ago, "total_deleted": 0}),
        encoding="utf-8",
    )

    session = MagicMock()
    worker = RetentionWorker(session, state_file=str(state_path))
    deleted = await worker.process_pending()

    assert deleted == 42
    assert cleanup_mock.call_count == 1


@pytest.mark.asyncio
async def test_corrupt_state_file_treated_as_missing(state_path: Path, cleanup_mock):
    """Garbage in state file → run cleanup (don't crash)."""
    state_path.write_text("not-json{{}", encoding="utf-8")

    session = MagicMock()
    worker = RetentionWorker(session, state_file=str(state_path))
    deleted = await worker.process_pending()

    assert deleted == 42  # ran
    assert cleanup_mock.call_count == 1


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_crash_worker(
    state_path: Path, monkeypatch
):
    """Если cleanup упал — worker возвращает 0, не пишет state, но
    не пробрасывает исключение (мы никогда не убиваем job_runner)."""
    fail = MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(rw_module, "cleanup_runtime_retention", fail)

    session = MagicMock()
    worker = RetentionWorker(session, state_file=str(state_path))
    deleted = await worker.process_pending()

    assert deleted == 0
    assert not state_path.exists()


@pytest.mark.asyncio
async def test_custom_interval(state_path: Path, cleanup_mock):
    """Сheck that custom `interval` is respected (для тестов рантайма)."""
    # 5 секунд назад, interval = 10s → должно скипнуть.
    five_sec_ago = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    state_path.write_text(
        json.dumps({"last_run_at": five_sec_ago, "total_deleted": 0}),
        encoding="utf-8",
    )

    session = MagicMock()
    worker = RetentionWorker(
        session, state_file=str(state_path), interval=timedelta(seconds=10)
    )
    deleted = await worker.process_pending()
    assert deleted == 0  # ещё рано

    # ещё через "5 секунд" (мокаем выставив state на 11 сек назад)
    eleven_sec_ago = (datetime.now(timezone.utc) - timedelta(seconds=11)).isoformat()
    state_path.write_text(
        json.dumps({"last_run_at": eleven_sec_ago, "total_deleted": 0}),
        encoding="utf-8",
    )
    deleted = await worker.process_pending()
    assert deleted == 42  # пора
