"""#405: деструктив на едином пути НЕ должен подтверждаться ДВАЖДЫ.

Баг (прод, «очисти список покупок», 2026-07-20): remove_shopping_items уже несёт bespoke-деструктив-
confirm (_confirm_wrap, специфичный «Я сейчас уберу «X»…»); при allowed_write=∅ (ярус б единого пути)
он оборачивался ЕЩЁ раз generic-confirm («Подтверди, пожалуйста: сделать это изменение?») → ДВА
interrupt'а на один вызов. Фикс: ярус б пропускает уже-bespoke-подтверждённый инструмент как есть
(маркер metadata.sreda_bespoke_confirm — на ФАКТ обёртки, не на имя), иначе generic-wrap как был
(незамаркированный деструктив всё равно получает confirm — тихой мутации нет).
"""
from __future__ import annotations

from langchain_core.tools import StructuredTool

from sreda.runtime.react_loop import (
    _INLINE_BESPOKE_CONFIRM,
    _apply_unified_policy,
    _confirm_wrap,
    _mark_bespoke_confirm,
)


def _tool(name):
    def _f(q: str = "", **kw):
        return f"{name}-done"
    return StructuredTool.from_function(func=_f, name=name, description=name)


def test_destructive_bespoke_confirm_not_double_wrapped_405():
    """remove_shopping_items уже bespoke-confirm-обёрнут → ярус б отдаёт как есть (одиночный
    специфичный confirm), НЕ оборачивает вторым generic-confirm (иначе двойное подтверждение)."""
    inner = _tool("remove_shopping_items")  # write / shopping, in _CONFIRM_PHRASE
    wrapped = _confirm_wrap(inner, "уберу «молоко» из списка покупок")  # bespoke destructive confirm
    out = _apply_unified_policy([wrapped], allowed_read=["web"], allowed_write=[])  # ярус б
    assert out == [wrapped]  # identity: НЕ обёрнут вторым confirm


def test_confirm_wrap_sets_bespoke_marker_405():
    """_confirm_wrap ставит маркер фактической обёртки (metadata.sreda_bespoke_confirm) — на него
    ключуется гейт яруса б (не на имя инструмента)."""
    wrapped = _confirm_wrap(_tool("delete_recipe"), "удалю рецепт «Борщ»")
    assert (getattr(wrapped, "metadata", None) or {}).get("sreda_bespoke_confirm") is True


def test_unmarked_destructive_still_gets_confirm_405():
    """Защита от тихой записи: деструктив-инструмент БЕЗ bespoke-маркера (гипотетич. необёрнутый путь)
    на ярусе б всё равно получает generic-confirm — гейт на МАРКЕР, не на имя."""
    bare = _tool("remove_shopping_items")  # НЕ обёрнут → нет маркера
    out = _apply_unified_policy([bare], allowed_read=["web"], allowed_write=[])
    assert len(out) == 1 and out[0] is not bare  # generic-обёрнут (безопасно, без тихой мутации)


def test_nondestructive_write_still_candidate_405():
    """Регресс-guard: не-деструктив write (add_task, нет bespoke-confirm) → generic-кандидат как был."""
    t = _tool("add_task")
    out = _apply_unified_policy([t], allowed_read=["web"], allowed_write=[])
    assert len(out) == 1 and out[0] is not t  # generic-обёрнут (фикс не тронул)


def test_signaled_destructive_direct_single_confirm_405():
    """Ярус (а): деструктив с доменом В allowed_write → прямой (identity) уже сейчас — одиночный
    bespoke-confirm. Фикс делает ярус (б) для деструктива таким же (паритет)."""
    wrapped = _confirm_wrap(_tool("remove_shopping_items"), "уберу «хлеб»")
    out = _apply_unified_policy([wrapped], allowed_read=["web", "shopping"], allowed_write=["shopping"])
    assert out == [wrapped]  # identity в обоих ярусах → всегда РОВНО один confirm


# ── R2 (sol/terra MAJOR+MINOR): inline-деструктивы с ВСТРОЕННЫМ interrupt() ──────────────────
# cancel_reminder/cancel_task/delete_task несут confirm В ТЕЛЕ (не через _confirm_wrap). До R2
# они на ярусе (б) получали второй generic-confirm → двойное подтверждение (фикс #405 их не покрывал).


def test_inline_bespoke_destructive_marked_single_confirm_405():
    """R2: помеченный _mark_bespoke_confirm inline-деструктив на ярусе (б) → identity (РОВНО один
    confirm, свой inline), НЕ оборачивается вторым generic-confirm. Так их метит сборка bespoke."""
    for name in _INLINE_BESPOKE_CONFIRM:
        marked = _mark_bespoke_confirm(_tool(name))
        assert (getattr(marked, "metadata", None) or {}).get("sreda_bespoke_confirm") is True
        out = _apply_unified_policy([marked], allowed_read=["web"], allowed_write=[])
        assert out == [marked], f"{name}: ожидался identity (одиночный confirm) на ярусе (б)"


def test_inline_bespoke_destructive_unmarked_double_confirm_regression_405():
    """R2 RED-guard: тот же inline-деструктив БЕЗ пометки на ярусе (б) уходит в generic-confirm —
    это и есть двойное подтверждение (свой inline + generic), что чинит #405. Пометка обязательна."""
    for name in _INLINE_BESPOKE_CONFIRM:
        bare = _tool(name)  # без маркера — как было до R2
        out = _apply_unified_policy([bare], allowed_read=["web"], allowed_write=[])
        assert len(out) == 1 and out[0] is not bare, f"{name}: без маркера должен быть generic-wrap"


def test_bespoke_marker_requires_true_not_truthy_405():
    """R2 (sol/terra MINOR): гейт проверяет `is True`, не truthy — строка «false» в metadata НЕ
    отключает generic-confirm (иначе будущая копия metadata с truthy-значением обошла бы confirm)."""
    faux = _tool("remove_shopping_items").model_copy(
        update={"metadata": {"sreda_bespoke_confirm": "false"}})  # truthy-строка, но не True
    out = _apply_unified_policy([faux], allowed_read=["web"], allowed_write=[])
    assert len(out) == 1 and out[0] is not faux  # generic-обёрнут (маркер != True → не bespoke)


def test_inline_bespoke_set_is_core_write_no_wrapper_overlap_405():
    """R2 дрейф-guard: имена _INLINE_BESPOKE_CONFIRM — реально core write-деструктивы и НЕ пересекаются
    с _CONFIRM_PHRASE (обёрточный механизм). Пересечение = двойная пометка/конфликт двух confirm-путей."""
    from sreda.runtime.react_loop import _CONFIRM_PHRASE, _CORE_TOOL_NAMES
    from sreda.services.tool_schemas.families import TOOL_OP_CLASS
    assert _INLINE_BESPOKE_CONFIRM  # непусто
    for name in _INLINE_BESPOKE_CONFIRM:
        assert name in _CORE_TOOL_NAMES, f"{name} не в _CORE_TOOL_NAMES"
        assert TOOL_OP_CLASS.get(name) == "write", f"{name} не write-класса"
        assert name not in _CONFIRM_PHRASE, f"{name} и inline, и в _CONFIRM_PHRASE — конфликт механизмов"


def test_real_inline_destructives_single_interrupt_tier_b_405(db_session, monkeypatch):
    """R3 (sol/terra R1+R2 MINOR — «докажи инвариант на РЕАЛЬНЫХ инструментах, считая interrupt()»):
    cancel_task/delete_task/cancel_reminder из build_slice_tools на ярусе (б) единого пути
    (allowed_write=[]) дают РОВНО ОДИН фактический interrupt() (свой inline), НЕ два. Мутация только
    после «да»; «нет» → ноль мутаций. Не синтетика: реальные closure-инструменты через реальную политику."""
    from datetime import date, datetime, timezone
    from uuid import uuid4

    from sreda.db.models.housewife import FamilyReminder
    from sreda.db.models.tasks import Task
    from sreda.runtime import react_loop
    from sreda.runtime.planner.tool_runtime import ToolRuntimeContext, bind_tool_runtime
    from sreda.runtime.react_loop import _apply_unified_policy, build_slice_tools
    from tests.unit.conftest import seed_telegram_user

    counter = {"n": 0, "reply": "да"}

    def _fake_interrupt(*a, **k):
        counter["n"] += 1
        return counter["reply"]

    monkeypatch.setattr(react_loop, "interrupt", _fake_interrupt)  # module-state → monkeypatch (не присваивание)

    u = seed_telegram_user(db_session)
    db_session.commit()
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    tid_c, tid_d, tid_n = (f"task_{uuid4().hex[:18]}" for _ in range(3))
    for tid, title in ((tid_c, "отменяемая"), (tid_d, "удаляемая"), (tid_n, "сохраняемая")):
        db_session.add(Task(id=tid, tenant_id=u.tenant_id, user_id=u.user_id, title=title,
                            scheduled_date=date(2030, 6, 20), status="pending",
                            created_at=now, updated_at=now))
    rid = f"rem_{uuid4().hex[:18]}"
    when = datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc)
    db_session.add(FamilyReminder(id=rid, tenant_id=u.tenant_id, user_id=u.user_id, title="разминка",
                                  trigger_at=when, next_trigger_at=when, status="pending"))
    db_session.commit()
    tools = {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}

    def _tier_b(name):
        # ярус (б): write-домен инструмента вне allowed_write=[] → НЕ прямой; маркер is True → identity
        # (не generic-wrap). Если бы инструмент не был помечен — вернулась бы ДРУГАЯ (generic-обёрнутая) вещь.
        out = _apply_unified_policy([tools[name]], allowed_read=["web"], allowed_write=[])
        assert len(out) == 1 and out[0] is tools[name], \
            f"{name}: ярус (б) должен вернуть помеченный инструмент как есть (identity), иначе двойной confirm"
        return out[0]

    def _invoke(name, args):
        counter["n"] = 0
        # УНИКАЛЬНЫЙ operation_id на каждый вызов — иначе tombstone/replay вернул бы прошлый payload БЕЗ interrupt
        ctx = ToolRuntimeContext(operation_id=f"op_{name}_{uuid4().hex[:8]}", execution_id="e",
                                 step_id="s", tool_name=name, tenant_id=u.tenant_id,
                                 user_id=u.user_id, turn_key=f"tk_{uuid4().hex[:8]}")
        with bind_tool_runtime(ctx):
            return _tier_b(name).invoke(args)

    # «да» → РОВНО один interrupt (без второго generic-confirm) + мутация состоялась
    counter["reply"] = "да"
    _invoke("cancel_task", {"task_ref": tid_c})
    assert counter["n"] == 1, f"cancel_task: ожидался РОВНО один interrupt, было {counter['n']} (двойной confirm?)"
    db_session.expire_all()
    assert db_session.get(Task, tid_c).status == "cancelled", "cancel_task после «да» должен отменить"

    _invoke("delete_task", {"task_ref": tid_d})
    assert counter["n"] == 1, f"delete_task: ожидался один interrupt, было {counter['n']}"
    assert db_session.get(Task, tid_d) is None, "delete_task после «да» должен удалить"

    _invoke("cancel_reminder", {"reminder_ref": rid})
    assert counter["n"] == 1, f"cancel_reminder: ожидался один interrupt, было {counter['n']}"

    # «нет» → один interrupt, НОЛЬ мутаций
    counter["reply"] = "нет"
    _invoke("cancel_task", {"task_ref": tid_n})
    assert counter["n"] == 1, f"decline: ожидался один interrupt, было {counter['n']}"
    db_session.expire_all()
    assert db_session.get(Task, tid_n).status == "pending", "«нет» → задача НЕ должна отмениться"
