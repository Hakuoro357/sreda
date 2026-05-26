"""Housewife-skill composer templates (Sub-A5 foundation).

One Russian-language Jinja2 template per typical plan outcome. The
planner references these by ``template_id`` in its ``compose`` step;
the executor renders with values from ``execution_log_json``.

Naming convention: ``<scope>_<outcome>`` (e.g. ``shopping_added_ok``,
``recipe_not_found_ask_alt``). Lower-case + underscores so the planner
matches deterministically.

Voice: брунетка, на «ты», коротко, с лёгким эмодзи там где уместно.
Snapshot-tested in ``test_composer_registry.py::test_render_*`` so
voice drift is caught at PR time, not in production.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Shopping
# ---------------------------------------------------------------------------

_SHOPPING_ADDED_OK = "Записала: {{ items | join(', ') }}."

_SHOPPING_ADDED_EMPTY = (
    "Все уже было в списке — {{ duplicates | join(', ') }}. "
    "Ничего нового не добавила."
)

_SHOPPING_LIST_SHOW = (
    "В списке покупок ({{ count }}):"
    "{% for it in items %}"
    "\n• {{ it.raw_line }}"
    "{% endfor %}"
)

_SHOPPING_LIST_EMPTY = "Список покупок пуст."

# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

_REMINDER_SET_OK = "Напомню {{ when_phrase }}: {{ what }}."

_REMINDER_SKIPPED_PAST = (
    "Это время уже прошло ({{ trigger_at_local }}, {{ late_by_minutes }} минут назад). "
    "Назначить на завтра в это же время?"
)

_REMINDERS_LIST_SHOW = (
    "Напоминания ({{ count }}):"
    "{% for it in items %}"
    "\n• {{ it.raw_line }}"
    "{% endfor %}"
)

_REMINDERS_LIST_EMPTY = "Активных напоминаний нет."

# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------

_RECIPE_SHOW = "{{ recipe_text }}"

_RECIPE_NOT_FOUND_ASK_ALT = (
    "Не нашла рецепт «{{ query }}». Подскажи название точнее "
    "или скажи другое блюдо."
)

# ---------------------------------------------------------------------------
# Error / partial / fallback
# ---------------------------------------------------------------------------

_GENERIC_TOOL_ERROR = (
    "Что-то пошло не так с моей внутренней логикой "
    "({{ error_code }}). Попробуй ещё раз через минуту."
)

_PARTIAL_WITH_COMPOSE_ERROR = (
    "Сделала что просила"
    "{% if execution_summary %}: {{ execution_summary }}{% endif %}. "
    "С финальным сообщением что-то пошло не так, но действия выполнены."
)
"""Group 6.5 ``compose_failure_after_execution`` path — used when the
planner-chosen template_id became invalid between Phase B validation
and Phase D compose (registry deploy race). Tools already ran; we just
need to acknowledge without re-running."""


HOUSEWIFE_TEMPLATES: dict[str, str] = {
    # shopping
    "shopping_added_ok": _SHOPPING_ADDED_OK,
    "shopping_added_empty": _SHOPPING_ADDED_EMPTY,
    "shopping_list_show": _SHOPPING_LIST_SHOW,
    "shopping_list_empty": _SHOPPING_LIST_EMPTY,
    # reminders
    "reminder_set_ok": _REMINDER_SET_OK,
    "reminder_skipped_past": _REMINDER_SKIPPED_PAST,
    "reminders_list_show": _REMINDERS_LIST_SHOW,
    "reminders_list_empty": _REMINDERS_LIST_EMPTY,
    # recipes
    "recipe_show": _RECIPE_SHOW,
    "recipe_not_found_ask_alt": _RECIPE_NOT_FOUND_ASK_ALT,
    # error / fallback
    "generic_tool_error": _GENERIC_TOOL_ERROR,
    "partial_with_compose_error": _PARTIAL_WITH_COMPOSE_ERROR,
}


__all__ = ["HOUSEWIFE_TEMPLATES"]
