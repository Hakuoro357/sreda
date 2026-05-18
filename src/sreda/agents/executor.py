"""R-39: исполнитель плана действий.

Берёт ``ExecutionPlan`` от планировщика, диспатчит каждый ``ToolCall``
на соответствующую callable-функцию и собирает результаты в
``ToolJournal``. Покрывает:

- Идемпотентность: считает ключ по контракту, дедуплицирует записи в
  пределах журнала.
- Fail-fast по умолчанию: первое падение останавливает оборот (важно
  для Кати-сценария — если cancel прошёл, а schedule упал, мы не хотим
  продолжать как ни в чём не бывало).
- Defense-in-depth: незарегистрированный инструмент / пропущенное
  обязательное поле / исключение из callable — всё конвертируется в
  ``FAILURE`` запись, не в краш процесса.

Извлечение ``result_data`` для шаблона делегируется внешнему
``result_data_extractor(tool_name, args, raw_result) -> dict``. Если
не передан — используется сами ``args`` как fallback. Это позволяет
тестировать executor чисто (без знания о реальном формате результата
инструментов), а в проде передавать конкретный экстрактор.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from sreda.agents.contracts import (
    TOOL_CONTRACTS,
    ExecutionPlan,
    ResultKind,
    ToolCall,
    ToolJournalEntry,
)
from sreda.agents.idempotency import (
    MissingIdempotencyField,
    compute_idempotency_key,
)
from sreda.agents.journal import ToolJournal


logger = logging.getLogger(__name__)


ResultDataExtractor = Callable[[str, dict[str, Any], Any], dict[str, Any]]


@dataclass
class ExecutorResult:
    """Итог исполнения плана."""

    journal: ToolJournal
    halted_early: bool   # был ли план прерван fail-fast


# ─── Главная функция ──────────────────────────────────────────────────


def execute_plan(
    plan: ExecutionPlan,
    *,
    tenant_id: int,
    turn_id: str,
    tool_callables: dict[str, Callable[..., Any]],
    result_data_extractor: ResultDataExtractor | None = None,
    journal: ToolJournal | None = None,
    fail_fast: bool = True,
) -> ExecutorResult:
    """Исполнить план — пройти по каждому вызову и заполнить журнал.

    Args:
        plan: упорядоченный список запланированных вызовов.
        tenant_id: тенант (для idempotency key).
        turn_id: идентификатор хода (для PER_TURN).
        tool_callables: реальные callable-функции по имени инструмента.
        result_data_extractor: опциональный обработчик результата для
            подстановки в шаблон. Получает ``(tool_name, args, raw_result)``,
            возвращает dict для ``result_data``. По умолчанию — сами args.
        journal: внешний журнал (для накопления между ходами/раундами).
            Если ``None`` — создаётся новый.
        fail_fast: останавливать ли исполнение после первого FAILURE
            (по умолчанию True; для multi-action независимых вызовов
            может быть полезно False).

    Returns:
        ``ExecutorResult`` с заполненным журналом и флагом ``halted_early``.
    """
    journal = journal if journal is not None else ToolJournal()
    if result_data_extractor is None:
        result_data_extractor = _default_extractor

    halted = False

    for call in plan.calls:
        contract = TOOL_CONTRACTS.get(call.tool_name)
        if contract is None:
            _record_failure(
                journal, call, key=None,
                error="unknown_tool: контракт не зарегистрирован",
            )
            if fail_fast:
                halted = True
                break
            continue

        # Idempotency key
        try:
            key = compute_idempotency_key(tenant_id, turn_id, contract, call)
        except MissingIdempotencyField as exc:
            _record_failure(
                journal, call, key=None,
                error=f"missing_idempotency_field: {exc}",
            )
            if fail_fast:
                halted = True
                break
            continue

        # Dedup — этот же ключ уже выполнен в журнале (parallel hedge / retry)
        if journal.has_key(key):
            logger.info(
                "executor: дубль по idempotency_key — пропуск tool=%s key=%s",
                call.tool_name, key,
            )
            continue

        # Callable
        callable_fn = tool_callables.get(call.tool_name)
        if callable_fn is None:
            _record_failure(
                journal, call, key=key,
                error="no_callable_registered: исполнимая функция не передана",
            )
            if fail_fast:
                halted = True
                break
            continue

        # Реальный вызов
        try:
            raw_result = callable_fn(**call.args)
        except Exception as exc:  # noqa: BLE001 — любая ошибка инструмента → FAILURE
            logger.warning(
                "executor: tool=%s упал: %s: %s",
                call.tool_name, type(exc).__name__, exc,
            )
            _record_failure(
                journal, call, key=key,
                error=f"{type(exc).__name__}: {exc}",
            )
            if fail_fast:
                halted = True
                break
            continue

        # Извлечь result_data + добавить SUCCESS
        try:
            result_data = result_data_extractor(call.tool_name, call.args, raw_result)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "executor: result_data_extractor упал tool=%s: %s",
                call.tool_name, exc,
            )
            result_data = dict(call.args)

        entity_id = _extract_entity_id(contract.entity_id_field, call.args, raw_result)
        journal.append(ToolJournalEntry(
            tool_name=call.tool_name,
            action_index=call.action_index,
            result_kind=ResultKind.SUCCESS,
            result_data=result_data,
            entity_id=entity_id,
            idempotency_key=key,
        ))

    return ExecutorResult(journal=journal, halted_early=halted)


# ─── Внутренние помощники ─────────────────────────────────────────────


def _record_failure(
    journal: ToolJournal,
    call: ToolCall,
    *,
    key: str | None,
    error: str,
) -> None:
    """Добавить FAILURE-запись в журнал."""
    journal.append(ToolJournalEntry(
        tool_name=call.tool_name,
        action_index=call.action_index,
        result_kind=ResultKind.FAILURE,
        result_data={},
        error_message=error,
        idempotency_key=key,
    ))


def _default_extractor(
    tool_name: str,
    args: dict[str, Any],
    raw_result: Any,
) -> dict[str, Any]:
    """По умолчанию — сами args. Тесты + простые случаи."""
    return dict(args)


def _extract_entity_id(
    field_name: str | None,
    args: dict[str, Any],
    raw_result: Any,
) -> str | None:
    """Достать entity_id либо из args (часто), либо из результата (для create)."""
    if not field_name:
        return None
    # Сначала args (cancel/replace — id уже известен)
    if field_name in args and args[field_name] not in (None, ""):
        return str(args[field_name])
    # Затем raw_result, если это словарь
    if isinstance(raw_result, dict) and field_name in raw_result:
        value = raw_result[field_name]
        if value not in (None, ""):
            return str(value)
    return None
