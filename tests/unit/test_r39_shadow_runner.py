"""R-39 Slice 5: тесты shadow worker.

Pure-logic + DI-based threading тесты. Реальный threading.Thread не
запускаем — используем sync runner через DI parameter.
"""

from __future__ import annotations

from typing import Any

import pytest

from sreda.agents.r39_shadow_runner import (
    build_shadow_dry_run_callables,
    kick_off_shadow_thread,
)
from sreda.agents.r39_tool_callables import REQUIRED_TOOLS


# ─── build_shadow_dry_run_callables ──────────────────────────────────


def test_shadow_callables_cover_all_required_tools() -> None:
    callables = build_shadow_dry_run_callables()
    for name in REQUIRED_TOOLS:
        assert name in callables


def test_shadow_schedule_returns_entity_id_and_iso() -> None:
    callables = build_shadow_dry_run_callables()
    result = callables["schedule_reminder"](
        title="X", trigger_iso="2099-05-17T14:00:00+03:00",
    )
    assert result["entity_id"] == "shadow_rem"
    assert result["trigger_iso"] == "2099-05-17T14:00:00+03:00"
    assert result["status_token"] == "scheduled"


def test_shadow_update_preserves_reminder_id() -> None:
    callables = build_shadow_dry_run_callables()
    result = callables["update_reminder"](
        reminder_id="rem_real_42",
        trigger_iso="2099-05-17T15:00:00+03:00",
    )
    assert result["entity_id"] == "rem_real_42"
    assert result["trigger_iso"] == "2099-05-17T15:00:00+03:00"


def test_shadow_cancel_returns_minimal() -> None:
    callables = build_shadow_dry_run_callables()
    result = callables["cancel_reminder"](reminder_id="rem_x")
    assert result["raw_ok"] == "ok:cancelled"


def test_shadow_save_recipe() -> None:
    callables = build_shadow_dry_run_callables()
    result = callables["save_recipe"](title="Борщ", ingredients=[])
    assert result["entity_id"] == "shadow_rec"
    assert result["status_token"] == "saved"


def test_shadow_add_shopping_count_matches_input() -> None:
    callables = build_shadow_dry_run_callables()
    items = [{"title": "молоко"}, {"title": "хлеб"}, {"title": "сыр"}]
    result = callables["add_shopping_items"](items=items)
    assert result["items_added_count"] == 3


def test_shadow_add_shopping_empty_items() -> None:
    callables = build_shadow_dry_run_callables()
    result = callables["add_shopping_items"](items=[])
    assert result["items_added_count"] == 0


def test_shadow_complete_task() -> None:
    callables = build_shadow_dry_run_callables()
    result = callables["complete_task"](task_id="tsk_42")
    assert result["entity_id"] == "tsk_42"


# ─── kick_off_shadow_thread с DI runner ──────────────────────────────


def test_kick_off_with_sync_runner_calls_worker() -> None:
    """DI runner: проверяем что worker вызывается."""
    invocations: list[bool] = []

    def sync_runner(fn):
        invocations.append(True)
        # НЕ запускаем worker реально — он попытается ходить в БД
        # без mock'нутого session_factory

    kick_off_shadow_thread(
        user_text="х",
        tenant_id="42",
        user_id="user1",
        run_id="run-1",
        user_tz="Europe/Moscow",
        feature_key="housewife_assistant",
        runner=sync_runner,
    )
    assert invocations == [True]


def test_kick_off_worker_swallows_exception_when_session_factory_fails() -> None:
    """Если session_factory raises — worker не должен propagate (silent)."""
    def bad_session_factory():
        raise RuntimeError("DB unavailable")

    def sync_runner(fn):
        fn()  # запускаем worker в текущем потоке

    # Не должен бросать
    kick_off_shadow_thread(
        user_text="x",
        tenant_id="42",
        user_id="user1",
        run_id="run-broken",
        user_tz="Europe/Moscow",
        feature_key="housewife_assistant",
        session_factory=bad_session_factory,
        runner=sync_runner,
    )


def test_kick_off_default_runner_uses_threading(monkeypatch) -> None:
    """По умолчанию используется threading.Thread(daemon=True)."""
    started_threads: list = []

    class _FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.target = target
            self.daemon = daemon
            self.name = name
        def start(self):
            started_threads.append(self)

    monkeypatch.setattr(
        "sreda.agents.r39_shadow_runner.threading.Thread", _FakeThread,
    )

    def bad_session_factory():
        return None

    kick_off_shadow_thread(
        user_text="x",
        tenant_id="42",
        user_id="user1",
        run_id="run-default",
        user_tz="Europe/Moscow",
        feature_key="housewife_assistant",
        session_factory=bad_session_factory,
    )
    assert len(started_threads) == 1
    assert started_threads[0].daemon is True
    assert "run-default" in started_threads[0].name
