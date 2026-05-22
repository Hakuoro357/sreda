"""Smoke-тесты для задачи #59: проверяем что промпт-фиксы реально на месте.

Эти тесты — не функциональные (мы не вызываем LLM), а structural:
проверяем что новые строки/паттерны присутствуют в скомпонованном
системном промпте. Если кто-то случайно их удалит — тесты упадут.

Мотивирующий инцидент: 2026-05-21 09:24 MAX, LLM ответила «Записала в
дела по машине: — A — B — C» без вызова add_checklist_items. Промпт-фиксы
закрывают root cause (6 пробелов в системном промпте).
"""
from __future__ import annotations

from sreda.runtime.handlers import build_system_prompt


def test_ban_list_includes_zapisala_and_synonyms() -> None:
    """Codex MAJOR root-cause: «Записала» должна быть в ban-list
    _TOOL_DISCIPLINE_ADDENDUM. До задачи #59 этого слова там не было —
    LLM выбрала единственный незапрещённый claim-verb.
    """
    prompt = build_system_prompt("housewife_assistant")
    # Прошлые глаголы (regression — должны остаться)
    for old_verb in (
        "Готово", "Сделала", "Сохранила", "Поставила",
        "Создала", "Добавила", "Напомню",
    ):
        assert old_verb in prompt, f"existing verb {old_verb!r} missing"
    # Новые глаголы (задача #59)
    for new_verb in (
        "Записала", "Записал", "Записано",
        "Внесла", "Внесено",
        "Зафиксировала", "Зафиксировал",
        "Отметила", "Запланировала", "Обновила", "Удалила",
    ):
        assert new_verb in prompt, f"new verb {new_verb!r} missing from ban-list"


def test_dela_po_pattern_in_triggers() -> None:
    """HOUSEWIFE_FOOD addon теперь содержит «дела по X» паттерны как
    явные триггеры для checklist."""
    prompt = build_system_prompt("housewife_assistant")
    for trigger in (
        "дела по машине",
        "дела по детям",
        "дела по работе",
        "список покупок для отпуска",
    ):
        assert trigger in prompt, f"trigger {trigger!r} missing"


def test_add_checklist_items_one_call_recommended() -> None:
    """L1097-1101: для utterance с title+items промпт рекомендует ОДИН
    вызов add_checklist_items (tool сам создаст checklist). Двухшаговая
    операция create_checklist + add_checklist_items оставлена только
    для empty list use case."""
    prompt = build_system_prompt("housewife_assistant")
    # Должна быть явная инструкция «ОДИН вызов» или «САМ создаст»
    assert "ОДИН вызов" in prompt or "САМ создаст checklist" in prompt
    # И явное «дела по машине» как пример one-call
    assert "Дела по машине" in prompt or "дела по машине" in prompt


def test_write_intent_router_at_end_of_tool_discipline() -> None:
    """WRITE INTENT ROUTER должен быть в самом конце stable prompt
    (highest attention weight в transformer)."""
    prompt = build_system_prompt("housewife_assistant")
    assert "WRITE INTENT ROUTER" in prompt
    # Router находится в последней четверти промпта (= в конце stable prompt)
    last_quarter = prompt[len(prompt) * 3 // 4:]
    assert "WRITE INTENT ROUTER" in last_quarter


def test_core_mentions_checklist_tools() -> None:
    """CORE L957 теперь упоминает add_checklist_items / show_checklist
    / list_checklists прямо в первичном overview tools."""
    prompt = build_system_prompt("housewife_assistant")
    # Эти tools должны быть упомянуты В ПЕРВОЙ половине промпта (= в CORE)
    half = len(prompt) // 2
    core_half = prompt[:half]
    assert "add_checklist_items" in core_half
    assert "show_checklist" in core_half
    assert "list_checklists" in core_half


def test_generic_chat_inherits_tool_discipline_not_housewife() -> None:
    """Generic chat (feature_key=None) НЕ должен наследовать housewife
    addon. Только CORE + SOUL + TOOL_DISCIPLINE.

    WRITE INTENT ROUTER должен быть в обоих (он в TOOL_DISCIPLINE,
    включая «дела по» примеры — это правильно, базовые intent-rules
    глобальные).
    Но housewife-only детали (план кроя примеры, plan_week_menu,
    recipe rules) — только в housewife.
    """
    generic = build_system_prompt(None)
    housewife = build_system_prompt("housewife_assistant")
    assert len(generic) < len(housewife), (
        f"generic prompt ({len(generic)} chars) should be shorter than "
        f"housewife ({len(housewife)} chars)"
    )
    # WRITE INTENT ROUTER должен быть в обоих (TOOL_DISCIPLINE global)
    assert "WRITE INTENT ROUTER" in generic
    assert "WRITE INTENT ROUTER" in housewife
    # Ban-list (Записала и т.п.) — в обоих (TOOL_DISCIPLINE)
    assert "Записала" in generic
    assert "Записала" in housewife
    # Housewife-only детали — только в housewife
    assert "КУДА СОХРАНЯТЬ СПИСОК" not in generic, (
        "housewife heading просочился в generic"
    )
    assert "КУДА СОХРАНЯТЬ СПИСОК" in housewife
    # Конкретный housewife-only пример заголовка checklist
    assert "План кроя" not in generic
    assert "План кроя" in housewife
