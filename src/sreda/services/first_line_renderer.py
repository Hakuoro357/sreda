"""R-39: детерминированный рендеринг первой строки подтверждения.

На вход — запись журнала + контекст хода. На выход — одна короткая
русская фраза, собранная из шаблона ``ToolContract`` с подставленными
полями. LLM не участвует: это нижний слой полу-шаблона, тот самый
который убирает целый класс confab-багов.

Главный API:

- ``render_first_line(entry, context) -> str`` — одна запись журнала
  в одну строку.
- ``render_journal(entries, context) -> str`` — склейка нескольких
  записей через ``\n``. Каждая запись остаётся в одной физической
  строке — это инвариант (см. ``sanitize_for_display``).

Выбор варианта шаблона делается через стабильный seed
``sha256(turn_id:tool_name:action_index)``. Эта стабильность нужна
parallel hedge'у — если ход исполняется дважды (один из претендентов
проигнорирован), пользователь не должен видеть две разные
подтверждающие формулировки.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from sreda.agents.contracts import (
    TOOL_CONTRACTS,
    ResultKind,
    ToolContract,
    ToolJournalEntry,
    TurnContext,
)
from sreda.services.sanitize import sanitize_for_display


logger = logging.getLogger(__name__)


# ─── Главный API ──────────────────────────────────────────────────────


def render_first_line(entry: ToolJournalEntry, context: TurnContext) -> str:
    """Из записи журнала — одна детерминированная строка.

    Алгоритм:
      1. Берём контракт по ``tool_name``. Если контракта нет —
         нейтральный ответ.
      2. По ``result_kind`` выбираем список шаблонов
         (success/failure/partial). PARTIAL без поддержки → fallback
         в failure.
      3. Стабильный seed → один из вариантов.
      4. Все строковые значения ``result_data`` пропускаем через
         ``sanitize_for_display`` (newlines/control chars/длина).
      5. ``str.format`` с защитой: при отсутствии ключа уходим в
         нейтральный ответ + лог.
      6. Финальный гарант: в строке нет ``\\n`` (одна запись = одна
         строка).
    """
    if not context.turn_id:
        # Не падаем — seed всё ещё детерминирован — но это симптом
        # потерянного контекста хода; стоит увидеть в мониторинге.
        logger.warning(
            "first_line_renderer: пустой turn_id tenant=%s tool=%s — "
            "выбор шаблона деградирован (все вызовы получат один индекс)",
            context.tenant_id, entry.tool_name,
        )

    contract = TOOL_CONTRACTS.get(entry.tool_name)
    if contract is None:
        logger.info(
            "first_line_renderer: контракт не найден tool=%s — generic ack",
            entry.tool_name,
        )
        return _generic_acknowledgement(entry.result_kind)

    variants = _pick_variants(contract, entry)
    if not variants:
        logger.warning(
            "first_line_renderer: пустой список шаблонов tool=%s kind=%s",
            entry.tool_name, entry.result_kind,
        )
        return _generic_acknowledgement(entry.result_kind)

    chosen = _select_variant(variants, context, entry)
    safe_data = _sanitize_result_data(entry.result_data)

    try:
        rendered = chosen.format(**safe_data)
    except (KeyError, IndexError) as exc:
        logger.warning(
            "first_line_renderer: пропущен плейсхолдер tool=%s kind=%s missing=%s — generic ack",
            entry.tool_name, entry.result_kind, exc,
        )
        return _generic_acknowledgement(entry.result_kind)

    # Инвариант: одна запись = одна строка
    if "\n" in rendered or "\r" in rendered:
        rendered = rendered.replace("\r", " ").replace("\n", " ")
        rendered = " ".join(rendered.split())

    return rendered


def render_journal(entries: list[ToolJournalEntry], context: TurnContext) -> str:
    """Несколько записей в одном ходе склеиваются переводом строки.

    Каждая запись остаётся одной физической строкой (см.
    ``render_first_line``). Живая фраза от LLM добавляется отдельно
    выше — она ОДНА на весь ход.
    """
    if not entries:
        return ""
    return "\n".join(render_first_line(e, context) for e in entries)


# ─── Внутренние помощники ─────────────────────────────────────────────


def _pick_variants(contract: ToolContract, entry: ToolJournalEntry) -> tuple[str, ...]:
    """Выбор подходящего списка вариантов.

    Защита от PARTIAL без поддержки → fallback в failure.

    R-39 R7-3: для FAILURE с явным ``error_code`` (в entry.error_code или
    result_data["error_code"]) — сначала пробуем ``failure_template_variants_by_code``;
    если для этого кода вариантов нет, fallback на generic failure_template_variants.
    """
    if entry.result_kind is ResultKind.SUCCESS:
        return contract.success_template_variants
    if entry.result_kind is ResultKind.PARTIAL and contract.supports_partial:
        return contract.partial_template_variants
    # FAILURE или PARTIAL-fallback
    error_code = (
        getattr(entry, "error_code", None)
        or (entry.result_data or {}).get("error_code")
    )
    if error_code:
        by_code = contract.failure_template_variants_by_code.get(error_code)
        if by_code:
            return by_code
    return contract.failure_template_variants


def _select_variant(
    variants: tuple[str, ...],
    context: TurnContext,
    entry: ToolJournalEntry,
) -> str:
    """Стабильный выбор: sha256(turn_id:tool_name:action_index) → индекс."""
    key = f"{context.turn_id}:{entry.tool_name}:{entry.action_index}".encode("utf-8")
    digest = hashlib.sha256(key).digest()
    seed = int.from_bytes(digest[:8], "big")
    return variants[seed % len(variants)]


def _sanitize_result_data(result_data: dict[str, Any]) -> dict[str, Any]:
    """Все значения приводим к безопасной строке перед форматированием.

    - ``str``: ``sanitize_for_display``.
    - ``None``: пустая строка (вместо текста «None» в подтверждении).
    - ``bool``: строка-литерал ``true``/``false`` (избегаем «True» с
      капитализацией в русской фразе) — на практике bool в template'ах
      не встречается, но защита бесплатна.
    - ``int``/``float``: ``str()``.
    - ``list``/``tuple``/``dict``: коэрсим через ``str()`` + sanitize, но
      это сигнал ошибки контракта — логируем WARN.
    - ``datetime``/прочие: ``str()`` + sanitize.
    """
    safe: dict[str, Any] = {}
    for key, value in result_data.items():
        if value is None:
            safe[key] = ""
        elif isinstance(value, str):
            safe[key] = sanitize_for_display(value)
        elif isinstance(value, bool):
            safe[key] = "true" if value else "false"
        elif isinstance(value, (int, float)):
            safe[key] = str(value)
        elif isinstance(value, (list, tuple, dict)):
            logger.warning(
                "first_line_renderer: коллекция в result_data key=%s type=%s — "
                "контракт инструмента должен возвращать скаляры",
                key, type(value).__name__,
            )
            safe[key] = sanitize_for_display(str(value))
        else:
            safe[key] = sanitize_for_display(str(value))
    return safe


def _generic_acknowledgement(result_kind: ResultKind) -> str:
    """Нейтральная фраза когда конкретный шаблон неприменим."""
    if result_kind is ResultKind.SUCCESS:
        return "Готово ✓"
    if result_kind is ResultKind.PARTIAL:
        return "Получилось не до конца — проверь, пожалуйста."
    return "Не получилось — попробуй ещё раз."
