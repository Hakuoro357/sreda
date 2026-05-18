"""R-39 Slice 3: фабрика tool callables для process_turn.

Принимает ``tools_list`` (результат ``build_housewife_tools``) и
возвращает ``dict[str, Callable]`` — формат, который ожидает
``r39_pipeline.process_turn(tool_callables=...)``.

Для каждого из 6 целевых tools создаётся wrapper который:

1. **Past-date preflight** (schedule_reminder / update_reminder):
   проверяет ``trigger_iso`` через ``is_past_iso`` ДО ``tool.invoke``.
   Если время в прошлом → ``R39ToolFailure(error_code="past_date")``,
   реальный tool НЕ вызывается. Renderer покажет специальный
   past_date failure template из `_UPDATE_REMINDER` / `_SCHEDULE_REMINDER`.

2. **side_effects_state["started"] = True ДО invoke** (Codex R6 CRIT):
   маркируем что попытка mutating call пошла — даже если invoke raises,
   мы знаем что side effect МОГ закоммититься. Caller (`_r39_try_live`)
   при exception проверяет этот флаг и НЕ fallback'ит в legacy если
   started=True (иначе legacy выполнит дубль).

3. **tool.invoke(kwargs)** — реальный вызов LangChain tool. Service
   commit'ит сам внутри.

4. **parse_tool_result_or_raise(tool_name, raw)** — строка результата
   → dict (ok) или raise R39ToolFailure (error/skipped).

5. **side_effects_state["count"] += 1 при ok** — для аудит и
   _persist_r39_journal_row.

REQUIRED_TOOLS — explicit список 6 целевых tools для R-39 pipeline.
Защита от ошибочного отсутствия в build_housewife_tools (warn + raise
R39ToolFailure(tool_not_registered) при попытке вызова).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from sreda.agents.r39_tool_adapter import (
    R39ToolFailure,
    is_past_iso,
    parse_tool_result_or_raise,
)


logger = logging.getLogger(__name__)


# R-39 R7-6 (Codex): explicit список 6 R-39 целевых tools.
# update_reminder (не replace_reminder — Codex R7 patch finalize).
REQUIRED_TOOLS: tuple[str, ...] = (
    "schedule_reminder",
    "cancel_reminder",
    "update_reminder",
    "save_recipe",
    "add_shopping_items",
    "complete_task",
)


# Tools у которых есть trigger_iso в args — past-date preflight применяется.
_TIME_BEARING_TOOLS: frozenset[str] = frozenset({
    "schedule_reminder",
    "update_reminder",
})


# ─── Главная фабрика ──────────────────────────────────────────────────


def build_r39_tool_callables(
    tools_list: list[Any],
    side_effects_state: dict[str, Any],
    *,
    past_date_grace_minutes: int = 2,
) -> dict[str, Callable[..., Any]]:
    """Собрать R-39 callables из списка LangChain tools.

    Args:
        tools_list: результат ``build_housewife_tools`` (или dry-run mock'ов).
        side_effects_state: словарь которому wrapper будет писать ключи
            ``started`` (bool) и ``count`` (int). Caller обнуляет до
            вызова ``process_turn``.
        past_date_grace_minutes: окно для preflight past-date check
            time-bearing tools.

    Returns:
        Словарь {tool_name: callable(**kwargs) -> dict | raises}.
        Содержит **все** ``REQUIRED_TOOLS`` — для отсутствующих в
        ``tools_list`` wrapper'ы возвращаются, но при invoke поднимают
        ``R39ToolFailure(tool_not_registered)`` (consistent failure
        flow, не KeyError).
    """
    side_effects_state.setdefault("started", False)
    side_effects_state.setdefault("count", 0)

    by_name = {getattr(t, "name", None): t for t in (tools_list or [])}

    # Defensive warning при отсутствии (Codex R7-6)
    missing = [n for n in REQUIRED_TOOLS if n not in by_name]
    if missing:
        logger.error(
            "R-39 tool_callables: tools missing from build_housewife_tools: %s",
            missing,
        )

    def make_wrapper(tool_name: str) -> Callable[..., Any]:
        tool_obj = by_name.get(tool_name)

        def wrapper(**kwargs: Any) -> dict[str, Any]:
            # Defensive: tool отсутствует в фабрике
            if tool_obj is None:
                raise R39ToolFailure(
                    error_code="tool_not_registered",
                    error_message=f"tool {tool_name!r} missing from build_housewife_tools",
                    raw="",
                )

            # Past-date preflight для time-bearing tools (Codex R7 MAJ).
            # ВАЖНО: до side_effects_state["started"]=True, чтобы fallback
            # в legacy остался безопасным если preflight отвергает запрос.
            if tool_name in _TIME_BEARING_TOOLS:
                trigger_iso = kwargs.get("trigger_iso")
                if trigger_iso and is_past_iso(
                    str(trigger_iso), grace_minutes=past_date_grace_minutes
                ):
                    raise R39ToolFailure(
                        error_code="past_date",
                        error_message=f"trigger_iso в прошлом: {trigger_iso}",
                        raw="",
                    )

            # Mark BEFORE invoke (Codex R6 CRIT — race window).
            side_effects_state["started"] = True

            raw = tool_obj.invoke(kwargs)
            parsed = parse_tool_result_or_raise(tool_name, raw)
            # parsed это dict для ok-case; raise для error/skipped
            side_effects_state["count"] = side_effects_state.get("count", 0) + 1
            return parsed

        return wrapper

    return {name: make_wrapper(name) for name in REQUIRED_TOOLS}
