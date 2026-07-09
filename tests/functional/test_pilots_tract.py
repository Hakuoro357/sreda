"""#133 фаза A — пилотные сценарии тракта (чеклист пп.1, 3, 5)."""
from __future__ import annotations

import pytest

from tests.functional.conftest import (  # noqa: F401
    assert_happy_invariants,
    db_session,
    make_planner_queue,
)


import contextlib
import logging as _logging


@contextlib.contextmanager
def _caplog_ctx():
    """Захват записей sreda-логгеров без pytest-фикстуры (async-тесты)."""
    records: list = []

    class _H(_logging.Handler):
        def emit(self, record):  # noqa: ANN001
            records.append(record)

    h = _H(level=_logging.WARNING)
    root = _logging.getLogger()
    root.addHandler(h)
    try:
        yield records
    finally:
        root.removeHandler(h)


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
                               tenant_id=harness.tenant,
                               title="Полить рассаду", position=1))
        sess.add(ChecklistItem(id="clitem_" + "ef" * 12, checklist_id=cid,
                               tenant_id=harness.tenant,
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
    # «Обрабатываю…» не висит: ack (первый send) ОБЯЗАН быть отредактирован
    # или удалён — без люков (Codex R1 medium + субагент: ветка
    # `or len(sends)>=2` пропускала ровно регрессию «финал новым сообщением,
    # ack висит»)
    assert harness.tg.sends, "ack не отправлялся"
    ack_mid = harness.tg.sends[0]["_mid"]
    finalized = (
        {d.get("message_id") for d in harness.tg.deletes}
        | {e.get("message_id") for e in harness.tg.edits}
    )
    assert ack_mid in finalized, (
        f"ack message_id={ack_mid} не финализирован (deletes/edits: {finalized})"
    )


@pytest.mark.asyncio
async def test_invalid_plan_retry_exhausted_visible(harness, run_turn) -> None:
    """Чеклист п.3а: все ответы планировщика невалидны → честный отказ
    пользователю + алерт + ERROR-фиксация (ничего тихого)."""
    import logging
    from sreda.services.composer.breakdown_messages import BREAKDOWN_POOL

    queue = make_planner_queue("это не json", "{однозначно мусор")
    logging.disable(logging.NOTSET)
    with _caplog_ctx() as records:
        await run_turn("покажи дела", queue, update_id=2)
    assert queue.calls["n"] >= 2, "повтор невалидного плана не случился"
    final = harness.tg.user_visible_final
    assert final, "пользователь остался без ответа"
    pool_or_fallback = final in BREAKDOWN_POOL or "не получилось" in final.lower()
    assert pool_or_fallback, f"отказ не честный: {final!r}"
    assert harness.alerts, "сбой плана обязан алертить владельцу"
    assert any(r.levelno >= logging.WARNING and r.name.startswith("sreda")
               for r in records), (
        "провал плана обязан быть записан в наш лог (WARNING/ERROR)"
    )


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



@pytest.mark.asyncio
async def test_network_ban_blocks_external_http(harness) -> None:
    """Чеклист п.8: тест блокировщика — наружу нельзя (включая DNS)."""
    import socket
    # Codex R2 high: ловим ИМЕННО гардовый AssertionError (generic
    # ConnectError мог бы означать «в CI просто нет сети»)
    with pytest.raises(AssertionError, match="запрещ"):
        socket.getaddrinfo("example.com", 443)
    import httpx
    with pytest.raises(BaseException) as ei:
        async with httpx.AsyncClient(timeout=2) as c:
            await c.get("http://example.com/")
    root = ei.value
    seen = set()
    while root is not None and id(root) not in seen:
        seen.add(id(root))
        if isinstance(root, AssertionError) and "запрещ" in str(root):
            break
        root = root.__cause__ or root.__context__
    assert root is not None, f"httpx упал не от гарда: {ei.value!r}"


@pytest.mark.asyncio
async def test_reminder_create_persists_and_replies(harness, run_turn) -> None:
    """Фаза A: write-семейство — напоминание создано В БД, имя и время
    в ответе (явная дата в args: механизм, не разбор времени)."""
    plan = _plan_one_step(
        "schedule_reminder",
        {"title": "Полить рассаду", "trigger_iso": "2027-07-01T09:00:00+03:00"},
        {"kind": "template", "template_id": "reminder_set_ok",
         "template_data": {"when_phrase": "1 июля в 9:00",
                            "what": "Полить рассаду"}},
        match={"status": "scheduled"},
    )
    await run_turn("напомни полить рассаду", make_planner_queue(plan),
                   update_id=5)
    assert_happy_invariants(
        harness, must_contain=("Полить рассаду", "1 июля в 9:00"),
    )
    sess = db_session(harness)
    try:
        from sreda.db.models.housewife import FamilyReminder
        rows = sess.query(FamilyReminder).all()
        assert len(rows) == 1 and "рассад" in (rows[0].title or "")
        # Codex R2 high: контракт целиком — статус и время персистнуты
        r = rows[0]
        r_status = getattr(r, "status", None)
        assert r_status in ("scheduled", "active", "pending"), (
            f"статус напоминания: {r_status!r}"
        )
        trig = getattr(r, "trigger_at", None) or getattr(r, "trigger_at_utc", None)
        assert trig is not None and "2027-07-01" in str(trig), (
            f"время триггера не персистнуто: {trig!r}"
        )
    finally:
        sess.close()
