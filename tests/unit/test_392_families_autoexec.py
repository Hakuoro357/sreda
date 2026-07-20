"""#392 autoexec-набор: граница реестра + двухключевой import-time гвард.

Владелец одобрил расширение (2026-07-20) тем же паттерном #389, НО R2+R3 Codex-ревью показало, что
каждая листовая семья задета взаимодействием → расширение сузилось до ПРОВЕРЕННОГО ЯДРА.

Финальный autoexec-набор: **add_shopping_items (#389), add_checklist_items (#392)** — оба аддитивны,
#393-заземлены (ответ называет результат), обратимы, страховка ловит промах (checklists/shopping имеют
read-cue). Деструктив/правки/статусы/create_ — НИКОГДА не autoexec.

ФОРКНУТО (отдельный follow-up):
  • save_recipe/save_recipes_batch — R3 terra MAJOR: НЕ подключены к #393-заземлению → autoexec-запись
    могла бы кончиться филлером; данными не доказаны. Вернуть после wiring в #393.
  • add_family_members — R2 sol MAJOR: household БЕЗ read-cue → autoexec-промах немеряем страховкой.
  • add_task — канонический candidate-пример в ~8 тестах #285/#316/#320/#321 → autoexec = churn.
  • save_core_fact/save_episode (память) — конфликт с дверью #319 sticky-by-use (НЕ с #363).
"""

from __future__ import annotations

import pytest
from langchain_core.tools import StructuredTool

from sreda.runtime.react_loop import (
    _UNIFIED_AUTOEXEC_OWNER_ALLOWLIST,
    _UNIFIED_AUTOEXEC_WRITE_TOOLS,
    _apply_unified_policy,
    _validate_unified_autoexec_registry,
)
from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST


def _tool(name):
    def _f(q: str = "", **kw):
        return f"{name}-done"
    return StructuredTool.from_function(func=_f, name=name, description=name)


_CORE_AUTOEXEC = frozenset({"add_shopping_items", "add_checklist_items"})

# форкнуто в отдельный follow-up — НЕ в autoexec (каждая семья задета ревью, см. модуль-докстринг):
_FORKED = ["save_recipe", "save_recipes_batch", "add_family_members", "add_task",
           "save_core_fact", "save_episode"]


def test_autoexec_set_is_exactly_core():
    """Реестр и owner-allowlist = РОВНО проверенное ядро {add_shopping_items, add_checklist_items}.
    Любое расширение должно осознанно менять ЭТОТ пин (защита от тихого дрейфа набора)."""
    assert _UNIFIED_AUTOEXEC_WRITE_TOOLS == _CORE_AUTOEXEC
    assert _UNIFIED_AUTOEXEC_OWNER_ALLOWLIST == _CORE_AUTOEXEC


@pytest.mark.parametrize("name", sorted(_CORE_AUTOEXEC))
def test_core_direct_at_empty_write(name):
    """aw=∅ (диктовка порциями без домен-слова) → прямой бинд, без confirm."""
    t = _tool(name)
    assert _apply_unified_policy([t], ["web"], []) == [t], name


# ─────────── деструктив/правки/статусы — НИКОГДА не autoexec ───────────

_NEVER_AUTOEXEC = [
    # деструктив
    "delete_task", "cancel_task", "remove_family_member", "delete_recipe",
    "clear_menu", "remove_shopping_items", "clear_bought_shopping",
    "delete_checklist_item", "archive_checklist", "cancel_reminder",
    # правки
    "update_task", "update_recipe", "update_family_member", "update_shopping_item",
    # смены статуса / перенос (destructive step)
    "mark_checklist_item_done", "complete_task", "mark_shopping_bought",
    "move_task_to_checklist",
    # cross-domain read-write
    "generate_shopping_from_menu",
]


@pytest.mark.parametrize("name", _NEVER_AUTOEXEC)
def test_never_autoexec_not_in_registry(name):
    assert name in TOOL_FAMILY_MANIFEST, f"{name} исчез из манифеста — обнови тест"
    assert name not in _UNIFIED_AUTOEXEC_WRITE_TOOLS, name
    assert name not in _UNIFIED_AUTOEXEC_OWNER_ALLOWLIST, name


@pytest.mark.parametrize("name", _FORKED)
def test_forked_not_in_autoexec(name):
    """Форкнутые семьи НЕ в реестре → их существующие тесты остаются зелёными (add_task=candidate в
    #285/#321; память под дверью #319; recipes/household — candidate под confirm)."""
    assert name not in _UNIFIED_AUTOEXEC_WRITE_TOOLS, name
    assert name not in _UNIFIED_AUTOEXEC_OWNER_ALLOWLIST, name


@pytest.mark.parametrize("name", ["delete_task", "remove_family_member", "delete_recipe",
                                  "remove_shopping_items", "delete_checklist_item"])
def test_destructive_stays_candidate_at_empty_write(name):
    """Деструктив при aw=∅ — кандидат под confirm (прежнее B2-поведение, не тронуто)."""
    t = _tool(name)
    out = _apply_unified_policy([t], ["web"], [])
    assert len(out) == 1 and out[0] is not t, name


# ─────────── двухключевой гвард ───────────

def test_registry_guard_accepts_core():
    """Текущий (ядро) реестр валиден: каждый член — манифест + write + аддитив-префикс (add_) + allowlist."""
    _validate_unified_autoexec_registry()
    for name in _CORE_AUTOEXEC:
        _validate_unified_autoexec_registry(frozenset({name}))


def test_registry_guard_rejects_non_additive_prefix():
    """Не-аддитив-префикс (деструктив/правка/create_/save_) падает на import-time гварде (R3 sol:
    префикс сужен до add_ — save_/create_ тоже отвергаются здесь, а не только на allowlist)."""
    for name in ("delete_task", "update_recipe", "create_checklist",
                 "create_memory_category", "save_recipe", "save_core_fact"):
        with pytest.raises(RuntimeError, match="аддитивн"):
            _validate_unified_autoexec_registry(frozenset({name}))


def test_registry_guard_rejects_additive_prefix_not_in_allowlist():
    """add_*-инструмент вне owner-allowlist (add_task/add_family_members — форк) → гвард ловит
    owner-allowlist'ом: расширение требует явного owner-решения, не только add_-префикса."""
    for name in ("add_task", "add_family_members"):
        with pytest.raises(RuntimeError, match="owner-allowlist"):
            _validate_unified_autoexec_registry(frozenset({name}))
