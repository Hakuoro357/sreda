"""R-39: разрешитель target reminder для correction-ходов.

«Нет, не на 2 а на 14» — пользователь имеет в виду напоминание, которое
ассистент только что поставил. Этот модуль pure-функцией находит target
из conversation history (НЕ из БД — план R-39 явно требует «target из
conversation, не DB-only»).

API:

    resolve_correction_target(user_text, history, *, lookback_turns=3)
        -> ResolvedCorrection | AmbiguousCorrection | NoCorrectionTarget

Алгоритм:

1. Берём ``lookback_turns`` последних ходов истории.
2. Собираем SUCCESS-записи инструментов которые **создают** или
   **изменяют** напоминание (``schedule_reminder``, ``replace_reminder``).
3. Исключаем сущности, отменённые позднее в той же истории (cancel'ом).
4. Один кандидат → ``ResolvedCorrection`` (используется для replace).
5. Несколько → ``AmbiguousCorrection`` (попросить уточнение у пользователя).
6. Ноль → ``NoCorrectionTarget``.

Не использует ни LLM, ни БД — детерминированная функция от истории.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from sreda.agents.contracts import (
    ConversationTurn,
    ResultKind,
)


# ─── Тип результата ───────────────────────────────────────────────────


@dataclass(frozen=True)
class ResolvedCorrection:
    """Однозначно определённый target напоминания."""

    target_entity_id: str
    target_title: str | None
    target_tool: str           # "schedule_reminder" | "replace_reminder"
    source_turn_id: str        # из какого хода вытащили


@dataclass(frozen=True)
class AmbiguousCorrection:
    """Несколько кандидатов — нужно уточнить у пользователя."""

    candidates: tuple[ResolvedCorrection, ...]
    reason: str


@dataclass(frozen=True)
class NoCorrectionTarget:
    """В истории нет подходящего напоминания для коррекции."""

    reason: str


ResolverResult = Union[ResolvedCorrection, AmbiguousCorrection, NoCorrectionTarget]


# Инструменты создающие/изменяющие напоминание — кандидаты на target
_MUTATING_REMINDER_TOOLS = frozenset({"schedule_reminder", "replace_reminder"})


# ─── Главная функция ──────────────────────────────────────────────────


def resolve_correction_target(
    user_text: str,
    conversation_history: list[ConversationTurn] | tuple[ConversationTurn, ...],
    *,
    lookback_turns: int = 3,
) -> ResolverResult:
    """Найти target для correction-хода.

    Args:
        user_text: текст пользователя текущего хода (зарезервирован для
            будущей логики — например, выбор по упомянутому времени или
            названию из user_text). В v1 не используется напрямую.
        conversation_history: история ходов в хронологическом порядке.
        lookback_turns: сколько последних ходов сканировать.

    Returns:
        Один из трёх типов результата.
    """
    _ = user_text  # зарезервировано для будущей логики (v1 не использует)

    if not conversation_history:
        return NoCorrectionTarget(reason="empty_history")

    recent = list(conversation_history)[-lookback_turns:]

    # Last-write-wins по entity_id: идём reversed (свежие первые),
    # первая встреченная запись определяет судьбу id. Если это cancel —
    # помечаем мёртвым (skip), если schedule/replace — добавляем кандидата.
    # Это правильно обрабатывает cancel→re-schedule того же id (re-schedule
    # был ПОЗЖЕ cancel в reverse-итерации он встретится РАНЬШЕ).
    candidates: list[ResolvedCorrection] = []
    seen_ids: set[str] = set()
    for turn in reversed(recent):
        for entry in turn.journal_entries:
            if entry.result_kind is not ResultKind.SUCCESS:
                continue
            eid = entry.entity_id
            if not eid:
                continue
            if eid in seen_ids:
                continue
            # Cancel: помечаем мёртвым, дальше старые упоминания того же
            # id игнорируем (они отменены этим cancel'ом).
            if entry.tool_name == "cancel_reminder":
                seen_ids.add(eid)
                continue
            # Schedule/replace: живой кандидат, фиксируем.
            if entry.tool_name not in _MUTATING_REMINDER_TOOLS:
                continue
            seen_ids.add(eid)
            title = entry.result_data.get("title") if entry.result_data else None
            candidates.append(ResolvedCorrection(
                target_entity_id=eid,
                target_title=str(title) if title else None,
                target_tool=entry.tool_name,
                source_turn_id=turn.turn_id,
            ))

    if not candidates:
        return NoCorrectionTarget(reason="no_mutable_reminder_in_history")
    if len(candidates) == 1:
        return candidates[0]
    return AmbiguousCorrection(
        candidates=tuple(candidates),
        reason=f"multiple_recent_reminders ({len(candidates)})",
    )
