"""#389: добавление в покупки на едином пути — БЕЗ candidate-confirm.

Прод-данные (react_turn_trace, 01–18.07.2026): продолжение диктовки списка покупок
(«1 литр молока», «Еще добавь в соль») не несёт императив+домен-слово в ОДНОМ сообщении →
compute_unified_policy даёт allowed_write=∅ → add_shopping_items биндился КАНДИДАТОМ под
generic-confirm («…Подтверждаешь?») на каждом пункте. Оба прод-кейса пользователь
подтвердил «да» = по калибровочному контракту #285 Фазы A (confirm_resolution=yes на
ярусе (б)) — зафиксированные промахи сигнала, чистое трение.

Фикс (#389): add_shopping_items — аддитивная, видимая, обратимая операция (позиция
владельца в issue) → прямой бинд ВСЕГДА (осознанное точечное исключение из пилляра
«нет молчаливой записи», только этот инструмент). Деструктив shopping
(remove_*/clear_*) и НЕ доказанные данными семьи/инструменты — под confirm как раньше.
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool

from sreda.runtime.react_loop import _apply_unified_policy
from sreda.runtime.react_policy import compute_unified_policy
from sreda.runtime.react_preflight import route_domains


def _tool(name):
    def _f(q: str = "", **kw):
        return f"{name}-done"
    return StructuredTool.from_function(func=_f, name=name, description=name)


def _policy(text):
    return compute_unified_policy(text, route_domains(text))


# ─────────── прод-репродукции (root cause #389) ───────────

def test_prod_case_bare_item_direct_add():
    """«1 литр молока» (T6/MAX 18.07, confirm→yes): ни императива, ни домен-слова →
    гейт write промахивается (документируем причину) — но add_shopping_items обязан
    биндиться ПРЯМЫМ (identity), а не кандидатом под confirm."""
    pol = _policy("1 литр молока")
    assert "shopping" not in pol["allowed_write"]  # причина: гейт не даёт shopping
    t = _tool("add_shopping_items")
    out = _apply_unified_policy([t], pol["allowed_read"], pol["allowed_write"])
    assert out == [t]  # identity: без confirm-обёртки


def test_prod_case_echo_add_salt_direct_add():
    """«Еще добавь в соль» (T6/MAX 18.07, confirm→yes): императив есть, домен-слова нет →
    allowed_write=∅ — add_shopping_items всё равно прямой."""
    pol = _policy("Еще добавь в соль")
    assert "shopping" not in pol["allowed_write"]
    t = _tool("add_shopping_items")
    out = _apply_unified_policy([t], pol["allowed_read"], pol["allowed_write"])
    assert out == [t]


def test_full_phrase_still_direct():
    """Полная фраза «добавь молоко в список покупок» шла прямым ярусом (а) до фикса —
    регрессия: остаётся прямой."""
    pol = _policy("добавь молоко в список покупок")
    assert "shopping" in pol["allowed_write"]  # штатный ярус (а)
    t = _tool("add_shopping_items")
    out = _apply_unified_policy([t], pol["allowed_read"], pol["allowed_write"])
    assert out == [t]


# ─────────── границы фикса: деструктив и чужие семьи НЕ трогаем ───────────

def test_shopping_destructive_and_edits_still_candidates():
    """Деструктив/правки shopping НЕ авто-исполняются: remove/clear/update/mark —
    кандидаты под confirm при allowed_write=∅ (чеклист приёмки #389: ни одна
    destructive-операция не теряет подтверждение)."""
    for name in ("remove_shopping_items", "clear_bought_shopping",
                 "update_shopping_item", "update_shopping_items_category",
                 "mark_shopping_bought"):
        t = _tool(name)
        out = _apply_unified_policy([t], ["shopping", "web"], [])
        assert len(out) == 1 and out[0] is not t, name  # обёрнут кандидатом
        assert out[0].name == name


def test_other_add_tools_still_candidates():
    """Другие аддитивные семьи данными НЕ доказаны (#389 — только покупки):
    add_task / add_checklist_items остаются кандидатами при allowed_write=∅."""
    for name in ("add_task", "add_checklist_items"):
        t = _tool(name)
        out = _apply_unified_policy([t], ["web"], [])
        assert len(out) == 1 and out[0] is not t, name


def test_generate_shopping_from_menu_read_gate_intact():
    """B2 CodexH R2: write=shopping/read=menu без menu-гранта — кандидат (read-гейт
    прямого яруса не ослаблен фиксом)."""
    t = _tool("generate_shopping_from_menu")
    out = _apply_unified_policy([t], ["web", "shopping"], ["shopping"])
    assert len(out) == 1 and out[0] is not t
