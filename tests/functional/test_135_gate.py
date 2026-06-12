"""#135 чеклист пп.1, 3, 5 — golden-инвариант гейта через тракт #133."""
from __future__ import annotations

import asyncio
import json

import pytest

from tests.functional.conftest import db_session, make_planner_queue  # noqa: F401
from tests.functional.test_pilots_tract import (  # noqa: F401
    _checklists_show_plan,
    _seed_checklist,
)


def _track_library_tasks(monkeypatch):
    import sreda.runtime.planner.plan_library as pl
    tracked: list[asyncio.Task] = []

    def tracking(coro, **kw):
        t = asyncio.create_task(coro, **kw)
        tracked.append(t)
        return t

    monkeypatch.setattr(pl, "_create_task", tracking)
    return tracked


def _lib_rows(harness):
    from sreda.db.models.plan_library import PlanLibraryEntry
    sess = db_session(harness)
    try:
        return sess.query(PlanLibraryEntry).all()
    finally:
        sess.close()


@pytest.mark.asyncio
async def test_plan_library_disabled_tenant_untouched(
    harness, run_turn, monkeypatch,
) -> None:
    """Чеклист п.1 (ядро, директива владельца): тенант ВНЕ гейта —
    промпт планировщика байт-в-байт, ноль записей, ноль фоновых задач."""
    tracked = _track_library_tasks(monkeypatch)
    _seed_checklist(harness)
    seen_prompts: list[str] = []
    base_queue = make_planner_queue(_checklists_show_plan())

    async def spying(prompt, **kw):
        seen_prompts.append(prompt)
        return await base_queue(prompt, **kw)

    spying.calls = base_queue.calls
    # гейт НЕ содержит tenant_1 (env не выставлен) — прод-дефолт
    await run_turn("покажи дела", spying)
    assert _lib_rows(harness) == [], "запись в библиотеку вне гейта"
    assert tracked == [], "фоновая задача библиотеки вне гейта"
    assert seen_prompts and "plan_examples" not in seen_prompts[0], (
        "в промпте не должно быть блока примеров (срез 4 не существует)"
    )


@pytest.mark.asyncio
async def test_candidate_recorded_and_detached_for_gated_tenant(
    harness, run_turn, monkeypatch,
) -> None:
    """Чеклист пп.2-3, 5: гейтнутый тенант — кандидат записан ФОНОМ
    (после ответа), тень в логе, промпт всё равно без примеров."""
    monkeypatch.setenv("SREDA_PLAN_LIBRARY_ENABLED_TENANTS", "tenant_1")
    from sreda.config.settings import get_settings
    get_settings.cache_clear()
    tracked = _track_library_tasks(monkeypatch)
    _seed_checklist(harness)
    seen_prompts: list[str] = []
    base_queue = make_planner_queue(_checklists_show_plan())

    async def spying(prompt, **kw):
        seen_prompts.append(prompt)
        return await base_queue(prompt, **kw)

    spying.calls = base_queue.calls
    await run_turn("покажи дела", spying)
    # ответ пользователю уже ушёл (run_turn дождался хода); задача
    # библиотеки — отдельная, доживаем явно
    assert tracked, "фоновая задача библиотеки не создана"
    await asyncio.wait_for(asyncio.gather(*tracked), timeout=10)
    rows = _lib_rows(harness)
    assert len(rows) == 1 and rows[0].status == "candidate"
    blob = rows[0].form_json
    assert "Полить рассаду" not in blob and "Дела на дачу" not in blob, (
        "PII в форме"
    )
    assert "show_checklist" in json.loads(rows[0].form_tags)
    assert "plan_examples" not in seen_prompts[0], (
        "тень не имеет права менять промпт"
    )
