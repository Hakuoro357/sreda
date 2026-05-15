"""R-30 option C: tests для write_intent_validator.

Coverage:
- READ_VERB_RE / WRITE_VERB_RE pattern accuracy на reference user texts
- classify_user_intent: read-only / write-only / compound / chitchat
- is_unsolicited_write: full matrix incl mutating-tool gate
- HOUSEWIFE_MUTATING_TOOL_NAMES consistency с WRITE_TOOLS_EXPECTED test_write_guard
"""

from __future__ import annotations

import pytest

from unittest.mock import patch

from sreda.services.write_intent_validator import (
    HOUSEWIFE_MUTATING_TOOL_NAMES,
    alert_if_unsolicited_write,
    classify_user_intent,
    is_unsolicited_write,
)


# ── classify_user_intent ───────────────────────────────────────────


@pytest.mark.parametrize("text,expected_read,expected_write", [
    # Pure read intents (the R-30 trigger case)
    ("Покажи мне весь крой на сегодня", True, False),
    ("Покажи список покупок", True, False),
    ("Что у меня в напоминаниях?", True, False),
    ("Какие задачи на сегодня?", True, False),
    ("Прочитай мне последний рецепт", True, False),
    ("Найди рецепт борща", True, False),
    ("Расскажи о моём меню", True, False),
    ("Сколько у меня тасков?", True, False),
    ("Show my checklist", True, False),
    # Codex R1 MINOR: extended read-verb coverage (was gap pre-fix)
    ("Дай список покупок", True, False),
    ("Выведи мне напоминания", True, False),
    ("Скинь рецепт борща", True, False),
    ("Пришли мне меню на неделю", True, False),
    ("Что по задачам на сегодня?", True, False),
    ("Что с меню?", True, False),
    ("Есть задачи на завтра?", True, False),
    # Pure write intents
    ("Добавь в список молоко", False, True),
    ("Запиши рецепт борща", False, True),
    ("Поставь напоминание на завтра", False, True),
    ("Сохрани этот рецепт", False, True),
    ("Создай чеклист «Уборка»", False, True),
    ("Напомни через 2 часа", False, True),  # Codex R1 MAJOR sentinel
    ("Запланируй меню на неделю", False, True),
    ("Отметь задачу выполненной", False, True),
    ("Купи молоко", False, True),
    ("Удали этот таск", False, True),
    ("Отмени напоминание", False, True),
    # Compound (multi-intent) — both flags True
    ("Покажи список и добавь туда рубашку", True, True),
    ("Какие напоминания? Поставь ещё одно на 19:00", True, True),
    # Chitchat / free-form (no verbs detected)
    ("Привет", False, False),
    ("Сегодня жарко", False, False),
    ("Спасибо", False, False),
])
def test_classify_user_intent(text: str, expected_read: bool, expected_write: bool) -> None:
    result = classify_user_intent(text)
    assert result["has_read_verb"] == expected_read, (
        f"text={text!r}: has_read_verb expected={expected_read} got={result['has_read_verb']}"
    )
    assert result["has_write_verb"] == expected_write, (
        f"text={text!r}: has_write_verb expected={expected_write} got={result['has_write_verb']}"
    )


def test_classify_empty_returns_false_false() -> None:
    assert classify_user_intent("") == {"has_read_verb": False, "has_write_verb": False}
    assert classify_user_intent("   ") == {"has_read_verb": False, "has_write_verb": False}


# ── is_unsolicited_write ───────────────────────────────────────────


def test_unsolicited_write_classic_case() -> None:
    """The R-30 trigger: «Покажи крой» + add_checklist_items → True."""
    assert is_unsolicited_write(
        "Покажи мне весь крой на сегодня",
        tool_name="add_checklist_items",
    ) is True


def test_unsolicited_write_pure_read_then_show_tool_not_flagged() -> None:
    """show_checklist on read-intent — legitimate, not flagged.

    is_unsolicited_write returns False if tool_name НЕ mutating.
    """
    assert is_unsolicited_write(
        "Покажи мне крой",
        tool_name="show_checklist",  # read-only — not in mutating set
    ) is False


def test_unsolicited_write_pure_write_legitimate() -> None:
    """Pure write intent + mutating tool → False (legitimate)."""
    assert is_unsolicited_write(
        "Добавь молоко в список",
        tool_name="add_shopping_items",
    ) is False


def test_unsolicited_write_compound_not_flagged() -> None:
    """Compound «покажи + добавь» → False (write-verb present overrides)."""
    assert is_unsolicited_write(
        "Покажи список и добавь рубашку",
        tool_name="add_shopping_items",
    ) is False


def test_unsolicited_write_chitchat_not_flagged() -> None:
    """No verbs (free-form text) → False (нет read-intent → не R-30 class)."""
    assert is_unsolicited_write(
        "Сегодня жарко в Москве",
        tool_name="save_recipe",
    ) is False


def test_unsolicited_write_empty_user_text() -> None:
    assert is_unsolicited_write("", tool_name="add_task") is False


def test_unsolicited_write_unknown_tool_not_flagged() -> None:
    """Unknown tool name (not в HOUSEWIFE_MUTATING_TOOL_NAMES) → False.

    Defends against future renaming — won't false-fire on read-only tools
    или unknown tools.
    """
    assert is_unsolicited_write(
        "Покажи мне крой",
        tool_name="show_checklist",  # not mutating
    ) is False
    assert is_unsolicited_write(
        "Покажи мне крой",
        tool_name="some_future_tool_not_registered",
    ) is False


# ── HOUSEWIFE_MUTATING_TOOL_NAMES consistency ──────────────────────


# ── alert_if_unsolicited_write (Codex R1 MINOR 3: integration helper) ──


def test_alert_helper_fires_for_unsolicited_write() -> None:
    """Read-intent + mutating tool + success → alert fires + returns True."""
    with patch(
        "sreda.services.admin_alerts.send_admin_alert"
    ) as mock_alert:
        result = alert_if_unsolicited_write(
            user_text="Покажи мне весь крой на сегодня",
            tool_name="add_checklist_items",
            result_str="ok:added:8 items",
            tenant_id="tenant_test",
            feature_key="housewife_assistant",
            iter_num=3,
            user_id="user_test",
            trace_id="trace_abc123",
        )
    assert result is True
    mock_alert.assert_called_once()
    kwargs = mock_alert.call_args.kwargs
    assert kwargs["severity"] == "INFO"
    assert "add_checklist_items" in kwargs["title"]
    assert kwargs["dedupe_key"].startswith("unsolicited_write:add_checklist_items:")
    assert "trace_abc123" in kwargs["body"]
    assert kwargs["extra_context"]["trace_id"] == "trace_abc123"
    assert kwargs["extra_context"]["tool"] == "add_checklist_items"


def test_alert_helper_skips_legitimate_write() -> None:
    """Write-intent + mutating tool → no alert, returns False."""
    with patch(
        "sreda.services.admin_alerts.send_admin_alert"
    ) as mock_alert:
        result = alert_if_unsolicited_write(
            user_text="Добавь молоко в список",
            tool_name="add_shopping_items",
            result_str="ok:saved",
            tenant_id="t",
            feature_key="hs",
            iter_num=1,
            user_id="u",
            trace_id="tr",
        )
    assert result is False
    mock_alert.assert_not_called()


def test_alert_helper_skips_compound_intent() -> None:
    """Compound («покажи + добавь») → no alert (legitimate multi-intent)."""
    with patch(
        "sreda.services.admin_alerts.send_admin_alert"
    ) as mock_alert:
        result = alert_if_unsolicited_write(
            user_text="Покажи список и добавь рубашку",
            tool_name="add_shopping_items",
            result_str="ok:saved",
            tenant_id="t", feature_key="hs", iter_num=1,
            user_id="u", trace_id="tr",
        )
    assert result is False
    mock_alert.assert_not_called()


def test_alert_helper_skips_non_mutating_tool() -> None:
    """Read-intent + read-tool → no alert (tool isn't mutating)."""
    with patch(
        "sreda.services.admin_alerts.send_admin_alert"
    ) as mock_alert:
        result = alert_if_unsolicited_write(
            user_text="Покажи список покупок",
            tool_name="list_shopping",  # read-only
            result_str="...",
            tenant_id="t", feature_key="hs", iter_num=1,
            user_id="u", trace_id="tr",
        )
    assert result is False
    mock_alert.assert_not_called()


def test_alert_helper_swallows_send_alert_exception() -> None:
    """send_admin_alert raises → helper swallows, returns False, doesn't crash."""
    with patch(
        "sreda.services.admin_alerts.send_admin_alert"
    ) as mock_alert:
        mock_alert.side_effect = RuntimeError("DB connection lost")
        # MUST NOT raise
        result = alert_if_unsolicited_write(
            user_text="Покажи мне крой",
            tool_name="add_checklist_items",
            result_str="ok:saved",
            tenant_id="t", feature_key="hs", iter_num=1,
            user_id="u", trace_id="tr",
        )
    assert result is False  # swallowed
    mock_alert.assert_called_once()


def test_alert_helper_handles_none_optional_params() -> None:
    """user_id / feature_key / trace_id могут быть None — helper не падает."""
    with patch(
        "sreda.services.admin_alerts.send_admin_alert"
    ) as mock_alert:
        result = alert_if_unsolicited_write(
            user_text="Покажи список",
            tool_name="save_recipe",
            result_str="ok",
            tenant_id="t",
            feature_key=None,
            iter_num=0,
            user_id=None,
            trace_id=None,
        )
    assert result is True
    mock_alert.assert_called_once()


# ── HOUSEWIFE_MUTATING_TOOL_NAMES consistency ──────────────────────


def test_housewife_mutating_set_matches_write_tools_expected() -> None:
    """Consistency: HOUSEWIFE_MUTATING_TOOL_NAMES в validator должно
    совпадать с WRITE_TOOLS_EXPECTED в test_write_guard.

    Если кто-то добавит mutating tool → должен обновить BOTH:
    - housewife_chat_tools.py (декоратор @_write_lc_tool)
    - write_intent_validator.HOUSEWIFE_MUTATING_TOOL_NAMES
    - test_write_guard.WRITE_TOOLS_EXPECTED
    """
    # Lazy import — test_write_guard может иметь свои deps
    from tests.unit.test_write_guard import WRITE_TOOLS_EXPECTED

    missing = WRITE_TOOLS_EXPECTED - HOUSEWIFE_MUTATING_TOOL_NAMES
    extra = HOUSEWIFE_MUTATING_TOOL_NAMES - WRITE_TOOLS_EXPECTED

    assert not missing, (
        f"Missing в HOUSEWIFE_MUTATING_TOOL_NAMES: {missing} "
        f"(в WRITE_TOOLS_EXPECTED, но не здесь — обнови validator)"
    )
    assert not extra, (
        f"Extra в HOUSEWIFE_MUTATING_TOOL_NAMES: {extra} "
        f"(нет в WRITE_TOOLS_EXPECTED — либо ошибка classification, либо "
        f"обнови test_write_guard.WRITE_TOOLS_EXPECTED)"
    )
