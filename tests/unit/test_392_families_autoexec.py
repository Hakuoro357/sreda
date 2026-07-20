"""#392 расширение: autoexec для ВСЕХ безопасных аддитивных/обратимых write-семей.

Владелец одобрил расширение (2026-07-20) тем же ПРОВЕРЕННЫМ паттерном #389 (реестр +
owner-allowlist + гейт конкурирующего домена + import-time валидатор) на аддитивные/видимые/
обратимые write. Диктовка порциями («ещё запиши…», дневник ~19 confirm/yes у соседа) больше
не упирается в лишний confirm. Деструктив (delete/clear/remove/archive), правки (update_*),
смены статуса (mark/complete) — НИКОГДА не autoexec.

Итоговый autoexec-набор: add_shopping_items (#389), add_checklist_items (#392),
add_family_members, save_recipe, save_recipes_batch (чистые листовые семьи).

ФОРКНУТО (отдельный follow-up, решение оркестратора 2026-07-20):
  • add_task — канонический пример candidate-паузы в ~8 тестах #285/#316/#320/#321; autoexec =
    непропорциональный churn confirm-сьюта (поведенчески безопасен, но форк).
  • save_core_fact/save_episode (память) — конфликт с #319 sticky-by-use (дверь серии): блокет-
    autoexec снёс бы «дверь закрылась → confirm» первой записи. НЕ путать с #363 (заземление
    ответа — другая ось). Развилка: заменять ли #319 на autoexec.
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
from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST, tool_write_domains


def _tool(name):
    def _f(q: str = "", **kw):
        return f"{name}-done"
    return StructuredTool.from_function(func=_f, name=name, description=name)


# инструмент → его домен (для конкурирующего кейса берём ЧУЖОЙ)
_NEW_AUTOEXEC = [
    "add_family_members", "save_recipe", "save_recipes_batch",
]

# форкнуто в отдельный follow-up — НЕ в autoexec:
#   add_task (churn confirm-сьюта #285/#321), память (конфликт #319 sticky)
_FORKED = ["add_task", "save_core_fact", "save_episode"]


@pytest.mark.parametrize("name", _NEW_AUTOEXEC)
def test_new_family_in_registry_and_allowlist(name):
    assert name in _UNIFIED_AUTOEXEC_WRITE_TOOLS, name
    assert name in _UNIFIED_AUTOEXEC_OWNER_ALLOWLIST, name


@pytest.mark.parametrize("name", _NEW_AUTOEXEC)
def test_new_family_direct_at_empty_write(name):
    """aw=∅ (диктовка порциями без домен-слова) → прямой бинд, без confirm."""
    t = _tool(name)
    out = _apply_unified_policy([t], ["web"], [])
    assert out == [t], name


@pytest.mark.parametrize("name", _NEW_AUTOEXEC)
def test_new_family_direct_on_own_domain(name):
    """aw={собственный домен} → прямой (штатный ярус а)."""
    dom = sorted(tool_write_domains(name))[0]
    t = _tool(name)
    out = _apply_unified_policy([t], [dom, "web"], [dom])
    assert out == [t], name


@pytest.mark.parametrize("name", _NEW_AUTOEXEC)
def test_new_family_confirm_on_competing_domain(name):
    """aw={ЧУЖОЙ домен} → кандидат под confirm (гейт конкурирующего домена жив)."""
    own = sorted(tool_write_domains(name))[0]
    competing = "shopping" if own != "shopping" else "checklists"
    t = _tool(name)
    out = _apply_unified_policy([t], [competing, "web"], [competing])
    assert len(out) == 1 and out[0] is not t, name


# ─────────── деструктив/правки/статусы каждой семьи — НИКОГДА не autoexec ───────────

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
    """add_task (churn confirm-сьюта) и память save_core_fact/save_episode (конфликт #319 sticky)
    ФОРКНУТЫ в отдельный follow-up → НЕ в реестре (их существующие тесты остаются зелёными:
    add_task=candidate в #285/#321, память под дверью #319)."""
    assert name not in _UNIFIED_AUTOEXEC_WRITE_TOOLS, name
    assert name not in _UNIFIED_AUTOEXEC_OWNER_ALLOWLIST, name


@pytest.mark.parametrize("name", ["delete_task", "remove_family_member", "delete_recipe",
                                  "remove_shopping_items", "delete_checklist_item"])
def test_destructive_stays_candidate_at_empty_write(name):
    """Деструктив при aw=∅ — кандидат под confirm (прежнее B2-поведение, не тронуто)."""
    t = _tool(name)
    out = _apply_unified_policy([t], ["web"], [])
    assert len(out) == 1 and out[0] is not t, name


# ─────────── двухключевой гвард на расширенном наборе ───────────

def test_registry_guard_accepts_all_new_members():
    """Текущий (расширенный) реестр валиден: каждый член — манифест + write + аддитив-префикс
    (add_/save_/create_) + owner-allowlist."""
    _validate_unified_autoexec_registry()
    for name in _NEW_AUTOEXEC:
        _validate_unified_autoexec_registry(frozenset({name}))


def test_registry_guard_rejects_destructive():
    """Деструктив (не аддитив-префикс) падает на import-time гварде."""
    with pytest.raises(RuntimeError, match="аддитивн"):
        _validate_unified_autoexec_registry(frozenset({"delete_task"}))
    with pytest.raises(RuntimeError, match="аддитивн"):
        _validate_unified_autoexec_registry(frozenset({"update_recipe"}))


def test_registry_guard_rejects_additive_prefix_not_in_allowlist():
    """create_* — аддитив-префикс, НО не owner-approved (развилка владельцу) → гвард ловит
    owner-allowlist'ом: расширение требует явного решения владельца, не только префикса."""
    with pytest.raises(RuntimeError, match="owner-allowlist"):
        _validate_unified_autoexec_registry(frozenset({"create_checklist"}))
    with pytest.raises(RuntimeError, match="owner-allowlist"):
        _validate_unified_autoexec_registry(frozenset({"create_memory_category"}))
