"""#133 фаза A, шаг 0 — red-first тесты прод-швов харнеса.

Чеклист приёмки #133, пп. 5 и 7: шов планировщика (ContextVar, не
глобальное состояние) и локальный шов отсоединённых задач (подмена
атрибута модуля asyncio была бы глобальной — Codex R4 плана).
"""
from __future__ import annotations

import asyncio
import inspect
import re
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Шов 1: _planner_call_override (ContextVar) доезжает до orchestrator_run
# ---------------------------------------------------------------------------


def _fake_pf() -> SimpleNamespace:
    return SimpleNamespace(
        run_id="r-seam", feature_key="housewife_assistant",
        user_text="привет", messages=[], tools_by_name={},
        _turn_msg_start_idx=0,
    )


@pytest.mark.asyncio
async def test_planner_override_is_passed_to_orchestrator(monkeypatch) -> None:
    from sreda.runtime import planner_chat
    from sreda.runtime.planner import orchestrator

    monkeypatch.setattr(planner_chat, "send_admin_alert",
                        lambda *a, **kw: None)
    captured: dict = {}

    async def fake_run(ctx, **kw):
        captured.update(kw)
        return SimpleNamespace(success=False, execution_plan=None,
                               final_attempt_no=1, error_summary="stub",
                               plan=None)

    monkeypatch.setattr(orchestrator, "run", fake_run)

    async def sentinel(prompt, **kw):  # noqa: ARG001
        raise AssertionError("не должен вызываться в этом тесте")

    token = planner_chat._planner_call_override.set(sentinel)
    try:
        await planner_chat.run_planner_chat_loop(
            session=None, action=SimpleNamespace(tenant_id="t-seam"),
            pf=_fake_pf(), context={},
        )
    finally:
        planner_chat._planner_call_override.reset(token)
    assert captured.get("call_planner_fn") is sentinel, (
        "override из ContextVar обязан прокидываться в orchestrator_run"
    )


@pytest.mark.asyncio
async def test_no_override_means_default_planner(monkeypatch) -> None:
    """Без override прод-поведение байт-в-байт: kwargs БЕЗ call_planner_fn."""
    from sreda.runtime import planner_chat
    from sreda.runtime.planner import orchestrator

    monkeypatch.setattr(planner_chat, "send_admin_alert",
                        lambda *a, **kw: None)
    captured: dict = {}

    async def fake_run(ctx, **kw):
        captured.update(kw)
        return SimpleNamespace(success=False, execution_plan=None,
                               final_attempt_no=1, error_summary="stub",
                               plan=None)

    monkeypatch.setattr(orchestrator, "run", fake_run)
    assert planner_chat._planner_call_override.get() is None
    await planner_chat.run_planner_chat_loop(
        session=None, action=SimpleNamespace(tenant_id="t-seam"),
        pf=_fake_pf(), context={},
    )
    assert "call_planner_fn" not in captured


# ---------------------------------------------------------------------------
# Шов 2: локальный _create_task в inbound-модулях (ТГ и МАКС)
# ---------------------------------------------------------------------------


def test_create_task_seam_is_local_and_used() -> None:
    import sreda.services.max_inbound as mi
    import sreda.services.telegram_inbound as ti

    assert ti._create_task is asyncio.create_task
    assert mi._create_task is asyncio.create_task
    for mod in (ti, mi):
        src = inspect.getsource(mod)
        calls = re.findall(r"asyncio\.create_task\(", src)
        assert not calls, (
            f"{mod.__name__}: точки вызова обязаны идти через локальный "
            f"_create_task (патч asyncio.create_task глобален)"
        )


@pytest.mark.asyncio
async def test_patched_seam_intercepts_task_creation(monkeypatch) -> None:
    """Подмена локального символа перехватывает создание задач, возвращая
    настоящий Task (контракт обёртки харнеса)."""
    import sreda.services.telegram_inbound as ti

    seen: list[str] = []

    def recording(coro, **kw):
        seen.append(kw.get("name") or getattr(coro, "__name__", "?"))
        return asyncio.create_task(coro, **kw)

    monkeypatch.setattr(ti, "_create_task", recording)

    async def _noop():
        return 42

    task = ti._create_task(_noop(), name="probe")
    assert isinstance(task, asyncio.Task)
    assert await task == 42
    assert seen == ["probe"]
