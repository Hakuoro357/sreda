"""Tests for _format_timeout_summary — chat turn timeout rescue.

2026-04-28: после incident tg_634496616 (LLM думал 131s при cap 90s,
turn aborted, но 2 add_task уже выполнились в БД) — handler теперь
выдаёт summary что было сделано вместо generic «не успел обдумать».
"""

from __future__ import annotations

from sreda.runtime.handlers import _format_timeout_summary


def test_empty_counts_returns_generic_message():
    """Если ни одного успешного tool-вызова не было — generic timeout."""
    text = _format_timeout_summary({})
    # Generic message — содержит «не успела сформулировать»
    assert "не успела сформулировать" in text or "Что-то успела сделать" in text


def test_single_add_task_groups_to_schedule_domain():
    text = _format_timeout_summary({"add_task": 1})
    assert "расписание" in text
    assert "успела сделать" in text.lower()
    # Без × N если count = 1
    assert "× 1" not in text


def test_two_add_tasks_show_count():
    """Incident kicker: 2 add_task → расписание × 2."""
    text = _format_timeout_summary({"add_task": 2})
    assert "расписание × 2" in text


def test_multiple_domains_combined():
    text = _format_timeout_summary({
        "add_task": 2,
        "save_recipe": 1,
        "add_shopping_items": 3,
    })
    # Должно быть все 3 домена в выводе, отсортированы по count убыванию
    assert "расписание × 2" in text
    assert "рецепты" in text and "× 2" not in text.split("рецепты")[1].split(",")[0]
    assert "список покупок × 3" in text
    # Сначала идёт самый «жирный» домен (3 добавок)
    assert text.find("список покупок") < text.find("расписание")
    assert text.find("расписание") < text.find("рецепты")


def test_unknown_tool_falls_back_to_generic():
    """Если tool не в _TOOL_TO_DOMAIN — generic message, не leakаем
    имя tool'а юзеру."""
    text = _format_timeout_summary({"future_unknown_tool_x": 5})
    assert "future_unknown_tool_x" not in text
    assert "Что-то успела сделать" in text


def test_mixed_known_and_unknown_only_shows_known():
    """Если часть известных + часть неизвестных — показываем только
    известные."""
    text = _format_timeout_summary({
        "add_task": 1,
        "totally_unknown_tool": 99,
    })
    assert "расписание" in text
    assert "totally_unknown_tool" not in text
    # 99 не должно появиться рядом с расписанием
    assert "× 99" not in text


def test_text_contains_apology_and_mini_app_hint():
    """Сообщение должно содержать (а) извинение за задержку (б) подсказку
    куда смотреть результат."""
    text = _format_timeout_summary({"add_task": 1})
    assert "Mini App" in text
    assert "запоздал" in text or "задержку" in text


def test_save_core_fact_groups_to_memory():
    text = _format_timeout_summary({"save_core_fact": 1, "save_episode": 1})
    # Оба mapping'ятся в "память" → объединяются в count=2
    assert "память × 2" in text


def test_shopping_items_and_generate_from_menu_share_domain():
    """generate_shopping_from_menu маппится в «список покупок» — если
    одновременно с add_shopping_items, объединяются."""
    text = _format_timeout_summary({
        "add_shopping_items": 1,
        "generate_shopping_from_menu": 1,
    })
    assert "список покупок × 2" in text
