"""Tests for detect_hallucinated_checklist_items.

Incident 2026-04-28 (tg_634496616): LLM писал в text-ответе «— ☐ Покрасить
дом, — ☐ Чинить забор, ...» хотя в БД был только «Покрасить дом».
Detector ловит такие галлюцинации.
"""

from __future__ import annotations

from sreda.services.llm import detect_hallucinated_checklist_items


def test_no_text_returns_empty():
    assert detect_hallucinated_checklist_items(
        "", last_show_checklist_result="anything"
    ) == []


def test_no_show_checklist_result_returns_empty():
    """Если show_checklist не вызывался — мы не можем валидировать,
    detector тихо пропускает."""
    text = "— ☐ Покрасить дом\n— ☐ Чинить забор"
    assert detect_hallucinated_checklist_items(
        text, last_show_checklist_result=None
    ) == []


def test_all_items_match_tool_result():
    """Все items в text есть в tool result — нет галлюцинаций."""
    text = (
        "Готово! Текущий чек-лист:\n"
        "— ☐ Покрасить дом\n"
        "— ☐ Чинить забор\n"
    )
    tool = (
        "# Глобальные дела на даче (checklist_xxx)\n"
        "[clitem_a] ☐ Покрасить дом\n"
        "[clitem_b] ☐ Чинить забор\n"
    )
    assert detect_hallucinated_checklist_items(
        text, last_show_checklist_result=tool
    ) == []


def test_item_in_text_but_not_in_tool_result():
    """Точный сценарий incident'а: бот выдумал «Чинить забор», а в
    tool result его нет."""
    text = (
        "Готово! Текущий чек-лист:\n"
        "— ☐ Покрасить дом\n"
        "— ☐ Чинить забор\n"
        "— ☐ Сделать забор\n"
        "— ☐ Разобрать кучу глины\n"
    )
    tool = (
        "# Глобальные дела на даче (checklist_xxx)\n"
        "[clitem_a] ☐ Покрасить дом\n"
    )
    hallucinated = detect_hallucinated_checklist_items(
        text, last_show_checklist_result=tool
    )
    assert "Чинить забор" in hallucinated
    assert "Сделать забор" in hallucinated
    assert "Разобрать кучу глины" in hallucinated
    assert "Покрасить дом" not in hallucinated


def test_case_insensitive_and_whitespace_match():
    """«покрасить  дом» = «Покрасить дом» (фaнки whitespace + регистр)."""
    text = "— ☐ покрасить  ДОМ"
    tool = "[clitem_a] ☐ Покрасить дом"
    assert detect_hallucinated_checklist_items(
        text, last_show_checklist_result=tool
    ) == []


def test_done_marker_also_matches():
    """☑ items тоже валидируются."""
    text = "— ☑ Покрасить дом"
    tool = "[clitem_a] ☑ Покрасить дом"
    assert detect_hallucinated_checklist_items(
        text, last_show_checklist_result=tool
    ) == []


def test_text_without_checklist_lines_returns_empty():
    """Если в text нет строк с галочками — нечего валидировать."""
    text = "Понятно, я записала эту задачу. Что-то ещё?"
    tool = "[clitem_a] ☐ X"
    assert detect_hallucinated_checklist_items(
        text, last_show_checklist_result=tool
    ) == []


def test_multiple_marker_styles_in_tool_result():
    """Tool result может быть в разных форматах — markdown vs
    простой `[clitem_xxx] ☐ X`."""
    text = "— ☐ Покрасить дом"
    # Альтернативный формат: «- ☐ Покрасить дом»
    tool = "- ☐ Покрасить дом"
    assert detect_hallucinated_checklist_items(
        text, last_show_checklist_result=tool
    ) == []


def test_empty_titles_not_counted():
    """Пустые маркеры (e.g. «☐ » без текста) — не валидируем."""
    text = "— ☐ \n— ☐ Покрасить дом"
    tool = "[clitem_a] ☐ Покрасить дом"
    # Пустой title — игнорируем
    result = detect_hallucinated_checklist_items(
        text, last_show_checklist_result=tool
    )
    assert "Покрасить дом" not in result
