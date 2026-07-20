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

from sreda.runtime.react_loop import _apply_unified_policy, _confirm_wrap


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
