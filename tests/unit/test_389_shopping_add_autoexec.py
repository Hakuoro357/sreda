"""#389: добавление в покупки на едином пути — БЕЗ candidate-confirm.

Прод-данные (react_turn_trace, 01–18.07.2026): продолжение диктовки списка покупок
(«1 литр молока», «Еще добавь в соль») не несёт императив+домен-слово в ОДНОМ сообщении →
compute_unified_policy даёт allowed_write=∅ → add_shopping_items биндился КАНДИДАТОМ под
generic-confirm («…Подтверждаешь?») на каждом пункте. Оба прод-кейса пользователь
подтвердил «да» = по калибровочному контракту #285 Фазы A (confirm_resolution=yes на
ярусе (б)) — зафиксированные промахи сигнала, чистое трение.

Фикс (#389, R2): add_shopping_items — аддитивная, видимая, обратимая операция (позиция
владельца в issue) → прямой бинд при ОТСУТСТВИИ конкурирующего write-домена роутера
(aw ⊆ write-доменов инструмента; субагент R1 MAJOR: «добавь в список дел купить молоко»
даёт aw={checklists} → кандидат+confirm примиряет расхождение «роутер vs модель»).
Осознанное точечное исключение из пилляра «нет молчаливой записи», только этот
инструмент (двухключевой import-time гвард: реестр + owner-allowlist). Деструктив
исключение #389 НЕ получает: его candidate-confirm при aw=∅ — прежнее B2-поведение,
не гарантия этого фикса; при aw={shopping} деструктив идёт штатным прямым ярусом (а),
где его страхует собственный destructive-confirm (_confirm_wrap).
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
    """Деструктив/правки shopping исключение #389 НЕ получают: remove/clear/update/mark
    при allowed_write=∅ — кандидаты под confirm (прежнее B2-поведение, фикс его не
    трогает; чеклист приёмки #389: ни одна destructive-операция не теряет
    подтверждение относительно поведения до фикса)."""
    for name in ("remove_shopping_items", "clear_bought_shopping",
                 "update_shopping_item", "update_shopping_items_category",
                 "mark_shopping_bought"):
        t = _tool(name)
        out = _apply_unified_policy([t], ["shopping", "web"], [])
        assert len(out) == 1 and out[0] is not t, name  # обёрнут кандидатом
        assert out[0].name == name


def test_non_additive_writes_still_candidates():
    """Write-инструменты ВНЕ autoexec-реестра (правки/статусы) остаются кандидатами при
    allowed_write=∅. #392-расширение сделало autoexec ТОЛЬКО аддитивные (add_/save_);
    update_/mark_ — не аддитивны → confirm-кандидат (регрессия границы)."""
    for name in ("update_task", "mark_checklist_item_done", "update_shopping_item"):
        t = _tool(name)
        out = _apply_unified_policy([t], ["web"], [])
        assert len(out) == 1 and out[0] is not t, name


def test_generate_shopping_from_menu_read_gate_intact():
    """B2 CodexH R2: write=shopping/read=menu без menu-гранта — кандидат (read-гейт
    прямого яруса не ослаблен фиксом)."""
    t = _tool("generate_shopping_from_menu")
    out = _apply_unified_policy([t], ["web", "shopping"], ["shopping"])
    assert len(out) == 1 and out[0] is not t


# ─────────── R2 (субагент MAJOR): конкурирующий write-домен роутера ───────────

def test_competing_router_write_domain_restores_confirm():
    """Субагент R1 MAJOR: «добавь в список дел купить молоко» → роутер дал
    ПОЛОЖИТЕЛЬНЫЙ write-грант ДРУГОГО домена (checklists). Прямой autoexec
    add_shopping_items здесь молча писал бы в shopping вопреки роутеру
    (до #389 confirm примирял расхождение «роутер=checklists vs модель=shopping»).
    Autoexec ТОЛЬКО без конкурирующего write-домена → здесь кандидат под confirm."""
    txt = "добавь в список дел купить молоко"
    pol = _policy(txt)
    assert "checklists" in pol["allowed_write"]      # конкурирующий грант роутера
    assert "shopping" not in pol["allowed_write"]
    t = _tool("add_shopping_items")
    out = _apply_unified_policy([t], pol["allowed_read"], pol["allowed_write"])
    assert len(out) == 1 and out[0] is not t         # confirm-backstop восстановлен
    assert out[0].name == "add_shopping_items"


def test_competing_write_domain_synthetic_matrix():
    """Матрица условия autoexec: aw=∅ → прямой (прод-репро); aw={shopping} → прямой;
    aw={checklists} → кандидат (конкурирующий домен)."""
    t = _tool("add_shopping_items")
    out_empty = _apply_unified_policy([t], ["web"], [])
    assert out_empty == [t]
    out_own = _apply_unified_policy([t], ["shopping", "web"], ["shopping"])
    assert out_own == [t]
    out_comp = _apply_unified_policy([t], ["checklists", "web"], ["checklists"])
    assert len(out_comp) == 1 and out_comp[0] is not t


# ─────────── R2 (субагент MINOR-1): import-time гвард реестра ───────────

def test_autoexec_registry_guard_mechanism():
    """Гвард механизмом, не комментом: член реестра обязан быть в манифесте,
    write-класса и аддитивным (add_*). Неосторожная правка падает на импорте."""
    from sreda.runtime.react_loop import _validate_unified_autoexec_registry
    import pytest

    _validate_unified_autoexec_registry()  # текущий реестр валиден
    with pytest.raises(RuntimeError, match="манифест"):
        _validate_unified_autoexec_registry(frozenset({"no_such_tool_xyz"}))
    with pytest.raises(RuntimeError, match="write"):
        _validate_unified_autoexec_registry(frozenset({"list_shopping"}))  # read-класс
    with pytest.raises(RuntimeError, match="аддитивн"):
        _validate_unified_autoexec_registry(frozenset({"remove_shopping_items"}))  # деструктив
    # R2 sol MINOR: аддитив-префикс другого инструмента гвард ловит owner-allowlist'ом —
    # расширение реестра требует явного решения владельца, не только префикса. (#392: add_task
    # теперь в allowlist; для этой ветки берём create_checklist — аддитив-префикс, но не approved.)
    with pytest.raises(RuntimeError, match="owner-allowlist"):
        _validate_unified_autoexec_registry(frozenset({"create_checklist"}))
