"""R-30: WRITE_GUARD prepended to every mutating tool's description.

После prod confab trace_1be1a4c0f576464e (mimo вызвала add_checklist_items × 8
на read-intent «Покажи мне крой»), каждый write-tool в housewife_assistant
получил anti-confab guard в description через `_write_lc_tool` декоратор.

Тесты:
1. Все mutating tools (по списку) имеют WRITE_GUARD prefix.
2. Read-only tools (list_*, get_*, search_*, show_*, reply_with_buttons) НЕ
   имеют WRITE_GUARD prefix (избегаем false-positive отпугивание модели).
3. WRITE_GUARD wording содержит ключевые элементы (явный «read-команды НЕ повод»,
   conditional escape «спроси записать»).

Эти тесты — regression guard от случайного отката _write_lc_tool на @lc_tool
в будущих PR'ах.
"""

from __future__ import annotations

import pytest

from sreda.services.housewife_chat_tools import (
    WRITE_GUARD,
    build_housewife_tools,
)


WRITE_TOOLS_EXPECTED: set[str] = {
    # reminders
    "schedule_reminder", "update_reminder", "cancel_reminder",
    # onboarding (state-changing)
    "onboarding_answered", "onboarding_deferred", "onboarding_complete",
    # shopping
    "add_shopping_items", "mark_shopping_bought", "remove_shopping_items",
    "update_shopping_item", "update_shopping_items_category",
    "clear_bought_shopping",
    # recipes
    "save_recipe", "save_recipes_batch", "delete_recipe",
    # menu
    "plan_week_menu", "update_menu_item", "generate_shopping_from_menu",
    "clear_menu",
    # family
    "add_family_members", "update_family_member", "remove_family_member",
    # tasks
    "add_task", "update_task", "complete_task", "uncomplete_task",
    "cancel_task", "delete_task", "attach_reminder", "detach_reminder",
    # checklists (incl R-30 culprit add_checklist_items)
    "create_checklist", "add_checklist_items", "move_task_to_checklist",
    "mark_checklist_item_done", "delete_checklist_item", "archive_checklist",
    # R-33 (2026-05-15): task↔checklist link tools
    "link_task_to_checklist", "unlink_task",
}


READ_ONLY_TOOLS_EXPECTED: set[str] = {
    "list_reminders", "list_shopping", "search_recipes", "get_recipe",
    "list_menu", "list_family_members", "list_tasks",
    "list_checklists", "show_checklist",
    "reply_with_buttons",  # UI primitive, not state-changing
}


@pytest.fixture
def _tools_built():
    """Build the tool list with a dummy session/tenant. We only inspect
    `.description` / `.name` — runtime execution не required для этих
    metadata тестов."""
    from unittest.mock import MagicMock
    return build_housewife_tools(
        session=MagicMock(),
        tenant_id="t_test",
        user_id="u_test",
        pending_buttons_state={},
        embedding_client=None,
    )


def test_write_guard_constant_non_empty() -> None:
    """WRITE_GUARD constant has the marker prefix and key warnings."""
    assert WRITE_GUARD.startswith("[WRITE-TOOL]"), (
        "WRITE_GUARD must lead with [WRITE-TOOL] marker для LLM-парсинга"
    )
    assert "ТОЛЬКО когда пользователь ЯВНО" in WRITE_GUARD, (
        "WRITE_GUARD must contain explicit «ТОЛЬКО ... ЯВНО» rule"
    )
    assert "Read-команды" in WRITE_GUARD, (
        "WRITE_GUARD must mention read-verbs negatively"
    )
    assert "записать?" in WRITE_GUARD or "спроси" in WRITE_GUARD.lower(), (
        "WRITE_GUARD must offer a conditional escape («спроси» / «записать?»)"
    )


def test_all_mutating_tools_have_write_guard(_tools_built) -> None:
    """Каждый write-tool из expected списка должен иметь WRITE_GUARD prefix
    в description."""
    by_name = {t.name: t for t in _tools_built}

    missing_in_built = WRITE_TOOLS_EXPECTED - set(by_name.keys())
    assert not missing_in_built, (
        f"Expected write-tools отсутствуют в built tools: {missing_in_built}"
    )

    failures: list[str] = []
    for name in sorted(WRITE_TOOLS_EXPECTED):
        tool = by_name[name]
        desc = tool.description or ""
        if not desc.startswith("[WRITE-TOOL]"):
            failures.append(
                f"{name}: description does NOT start with [WRITE-TOOL] guard "
                f"(starts with: {desc[:60]!r})"
            )
    assert not failures, "\n".join(failures)


def test_read_only_tools_do_NOT_have_write_guard(_tools_built) -> None:
    """Read-only tools НЕ должны иметь WRITE_GUARD — guard на read tool'е
    может scare-off модель от legitimate read'ов."""
    by_name = {t.name: t for t in _tools_built}

    failures: list[str] = []
    for name in sorted(READ_ONLY_TOOLS_EXPECTED):
        if name not in by_name:
            # reply_with_buttons depends on pending_buttons_state — could be absent
            continue
        tool = by_name[name]
        desc = tool.description or ""
        if desc.startswith("[WRITE-TOOL]"):
            failures.append(
                f"{name}: read-only tool ОШИБОЧНО has WRITE_GUARD prefix"
            )
    assert not failures, "\n".join(failures)


def test_no_lc_tool_drift_on_known_write_tools() -> None:
    """Source-level guard: ни один из expected write-tool'ов не должен быть
    зарегистрирован через bare ``@lc_tool``. Этот тест ловит regression
    типа «новый PR заменил @_write_lc_tool обратно на @lc_tool»."""
    import inspect
    from sreda.services import housewife_chat_tools as mod

    src = inspect.getsource(mod.build_housewife_tools)
    failures: list[str] = []
    for name in sorted(WRITE_TOOLS_EXPECTED):
        good = f"@_write_lc_tool\n    def {name}("
        bad = f"@lc_tool\n    def {name}("
        if bad in src:
            failures.append(f"{name}: defined with bare @lc_tool — should be @_write_lc_tool")
        elif good not in src:
            failures.append(f"{name}: neither @_write_lc_tool nor @lc_tool decorator found")
    assert not failures, "\n".join(failures)


def test_built_tools_invariant_no_unclassified(_tools_built) -> None:
    """Codex+Xiaomi R1 MINOR: invariant test. Каждый built tool ДОЛЖЕН быть
    либо в WRITE_TOOLS_EXPECTED либо в READ_ONLY_TOOLS_EXPECTED. Если кто-то
    добавит новый mutating tool с bare ``@lc_tool`` и забудет обновить
    expected set'ы — этот тест поймает regression раньше прода.

    Эвристика-подсказка: если name матчит write-verb pattern (add/create/
    delete/update/remove/cancel/mark/clear/save/plan/move/attach/detach/
    archive/generate/complete/uncomplete/schedule) — fail с suggestion
    добавить в WRITE_TOOLS_EXPECTED + использовать ``@_write_lc_tool``.
    """
    import re
    WRITE_VERB_PATTERN = re.compile(
        r"^(add|create|delete|update|remove|cancel|mark|clear|save|plan|move|"
        r"attach|detach|archive|generate|complete|uncomplete|schedule|"
        r"onboarding)_"
    )

    known = WRITE_TOOLS_EXPECTED | READ_ONLY_TOOLS_EXPECTED
    failures: list[str] = []
    for tool in _tools_built:
        if tool.name in known:
            continue
        likely_write = bool(WRITE_VERB_PATTERN.match(tool.name))
        suggestion = (
            "looks like WRITE-tool (verb prefix matches) — add to "
            "WRITE_TOOLS_EXPECTED + use @_write_lc_tool decorator"
            if likely_write else
            "either add to READ_ONLY_TOOLS_EXPECTED (if read-only) "
            "or WRITE_TOOLS_EXPECTED (with @_write_lc_tool decorator)"
        )
        failures.append(f"  {tool.name!r}: NOT classified — {suggestion}")
    assert not failures, (
        "Unclassified tools detected (regression risk: new write-tool без guard):\n"
        + "\n".join(failures)
    )
