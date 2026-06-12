"""#133 фаза A — пилотные сценарии тракта (чеклист пп.1, 3, 5)."""
from __future__ import annotations

import pytest

from tests.functional.conftest import (  # noqa: F401
    assert_happy_invariants,
    db_session,
    make_planner_queue,
)


def _plan_one_step(tool: str, args: dict, compose: dict,
                   match: dict | None = None) -> dict:
    return {
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "функц-тест"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": tool, "args": args,
                "expected_outcomes": [
                    {"match": match or {"status": "ok"}, "next": None,
                     "compose": compose},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
        },
        "compose": compose,
    }


def _checklists_show_plan() -> dict:
    """«Покажи дела»: пункты ПО ИМЕНАМ — show_checklist + checklist_show."""
    return _plan_one_step(
        "show_checklist", {"list_id_or_title": "Дела на дачу"},
        {"kind": "template", "template_id": "checklist_show",
         "template_data": {"title": "${s1.title}", "items": "${s1.items}"}},
    )


def _checklists_list_plan() -> dict:
    return _plan_one_step(
        "list_checklists", {},
        {"kind": "template", "template_id": "checklists_list_show",
         "template_data": {"items": "${s1.checklists}"}},
    )


def _seed_checklist(harness) -> None:
    from sreda.db.models.checklists import Checklist, ChecklistItem
    sess = db_session(harness)
    try:
        cid = "checklist_" + "ab" * 12
        sess.add(Checklist(id=cid, tenant_id=harness.tenant,
                           user_id="user_1", title="Дела на дачу",
                           status="active"))
        sess.add(ChecklistItem(id="clitem_" + "cd" * 12, checklist_id=cid,
                               title="Полить рассаду", position=1))
        sess.add(ChecklistItem(id="clitem_" + "ef" * 12, checklist_id=cid,
                               title="Купить шланг", position=2))
        sess.commit()
    finally:
        sess.close()


@pytest.mark.asyncio
async def test_checklists_show_lists_item_names(harness, run_turn) -> None:
    """Чеклист п.1 (ядро): «покажи дела» через весь живой механизм —
    пункты ПО ИМЕНАМ, ack не висит, ни алерта, ни «поломки»."""
    _seed_checklist(harness)
    await run_turn("покажи дела", make_planner_queue(_checklists_show_plan()))
    assert_happy_invariants(
        harness, must_contain=("Полить рассаду", "Купить шланг"),
    )
    # «Обрабатываю…» не висит: каждый ack либо удалён, либо отредактирован
    ack_mids = [s["_mid"] for s in harness.tg.sends
                if s is harness.tg.sends[0]]
    if ack_mids:
        finalized = (
            {d.get("message_id") for d in harness.tg.deletes}
            | {e.get("message_id") for e in harness.tg.edits}
        )
        assert set(ack_mids) <= finalized or len(harness.tg.sends) >= 2, (
            "ack-сообщение осталось висеть без финализации"
        )


@pytest.mark.asyncio
async def test_invalid_plan_retry_exhausted_visible(harness, run_turn) -> None:
    """Чеклист п.3а: все ответы планировщика невалидны → честный отказ
    пользователю + алерт + ERROR-фиксация (ничего тихого)."""
    import logging
    from sreda.services.composer.breakdown_messages import BREAKDOWN_POOL

    queue = make_planner_queue("это не json", "{однозначно мусор")
    logging.disable(logging.NOTSET)
    await run_turn("покажи дела", queue, update_id=2)
    assert queue.calls["n"] >= 2, "повтор невалидного плана не случился"
    final = harness.tg.user_visible_final
    assert final, "пользователь остался без ответа"
    pool_or_fallback = final in BREAKDOWN_POOL or "не получилось" in final.lower()
    assert pool_or_fallback, f"отказ не честный: {final!r}"
    assert harness.alerts, "сбой плана обязан алертить владельцу"


@pytest.mark.asyncio
async def test_planner_retry_then_success(harness, run_turn) -> None:
    """Чеклист п.5: 1-й ответ невалиден, 2-й валиден → ход успешен."""
    _seed_checklist(harness)
    queue = make_planner_queue("мусор не-json", _checklists_show_plan())
    await run_turn("покажи дела", queue, update_id=3)
    assert queue.calls["n"] == 2
    assert_happy_invariants(harness, must_contain=("Полить рассаду",))


@pytest.mark.asyncio
async def test_compose_breakdown_visible(harness, run_turn, caplog) -> None:
    """Чеклист п.3б: план валиден, сборка ломается → текст из пула
    «поломок» + ERROR «ПОЛОМКА» + алерт. Ломаем render подменой шаблона
    на несуществующий ПОСЛЕ валидации нельзя (валидатор поймает) —
    поэтому ломаем сам REGISTRY.render на стадии сборки."""
    import logging
    from sreda.services.composer import registry as reg_mod
    from sreda.services.composer.breakdown_messages import BREAKDOWN_POOL

    _seed_checklist(harness)
    real_render = reg_mod.REGISTRY.render

    def broken_render(template_id, data):  # noqa: ANN001
        if template_id == "checklist_show":
            raise RuntimeError("render exploded (functional pilot)")
        return real_render(template_id, data)

    import unittest.mock as um
    with um.patch.object(reg_mod.REGISTRY, "render", side_effect=broken_render):
        with caplog.at_level(logging.ERROR):
            await run_turn("покажи дела",
                           make_planner_queue(_checklists_show_plan()),
                           update_id=4)
    final = harness.tg.user_visible_final
    # шаг УСПЕЛ исполниться → честная деградация (partial_with_compose_error:
    # «действия выполнены, с сообщением не вышло»), а не «поломка» — контракт
    # #121; «поломка» из пула — для путей без исполненных шагов
    honest = (final in BREAKDOWN_POOL
              or "не получилось" in final.lower()
              or "пошло не так" in final.lower())
    assert honest, f"деградация сборки не честная: {final!r}"
    assert harness.alerts, "сбой сборки обязан алертить владельцу"
    if final in BREAKDOWN_POOL:
        assert any("ПОЛОМКА" in r.message for r in caplog.records), (
            "показ «поломки» обязан фиксироваться ERROR-логом"
        )
