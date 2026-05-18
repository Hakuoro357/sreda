"""R-39 L4: пост-фактум аудит ответа пользователю.

После того как ответ отправлен, прогоняем его через
``detect_unbacked_claim`` (тот самый pure-детектор который был primary
защитой до R-39). Теперь он в роли последнего safety net — если
структурные слои L1 (системный промпт), L2 (типизированный вывод
планировщика) и L3 (детерминированная первая строка + замок живой
фразы) пропустили confab, он ловит и:

1. Алертит админ-канал (chat_id=352612382) с деталями: что был claim,
   какие tools реально вызвались, какой текст.
2. Готовит ``correction_pending`` — короткое системное сообщение,
   которое инжектится в next turn как пометка «прошлый ответ не
   отразил реальное состояние». Это даёт LLM явный сигнал признать
   ошибку перед пользователем.

Аудит — асинхронный (вызывается ПОСЛЕ отправки ответа), он НЕ должен
блокировать UX. Реализация чистая DI: ``detector`` и ``admin_alert_fn``
инжектятся.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from sreda.agents.journal import ToolJournal


logger = logging.getLogger(__name__)


# Тип детектора confab'а — совместим с
# ``sreda.services.llm.detect_unbacked_claim``.
Detector = Callable[[str, set[str]], bool]
AdminAlertFn = Callable[[str], None]


@dataclass
class AuditResult:
    """Итог пост-фактум аудита."""

    is_unbacked: bool
    final_text: str
    called_tools: set[str] = field(default_factory=set)
    correction_pending: Optional[str] = None
    alert_sent: bool = False


# ─── Главная функция ──────────────────────────────────────────────────


def audit_response(
    *,
    final_text: str,
    journal: ToolJournal,
    detector: Detector,
    admin_alert_fn: AdminAlertFn | None = None,
    tenant_id: int | None = None,
    turn_id: str | None = None,
) -> AuditResult:
    """Прогнать финальный текст через детектор unbacked claim.

    Args:
        final_text: что бот реально отправил пользователю (первая строка
            + живая фраза, склейка после composer).
        journal: журнал действий хода — собирает tool_names вызванных
            реально (SUCCESS записи).
        detector: ``(text, called_tools) -> bool``. По контракту в проде
            это ``sreda.services.llm.detect_unbacked_claim``; здесь
            принимается через DI чтобы можно было тестировать без
            импорта тяжёлого модуля.
        admin_alert_fn: callback для алерта админу при обнаружении
            unbacked claim.
        tenant_id: для контекста алерта (без user_text — 152-ФЗ).
        turn_id: для контекста алерта.

    Returns:
        AuditResult с пометкой is_unbacked и опциональным
        ``correction_pending`` сообщением для injection в следующий ход.
    """
    called_tools = {
        e.tool_name for e in journal if e.tool_name and _is_real_call(e.tool_name)
    }

    try:
        is_unbacked = bool(detector(final_text or "", called_tools))
    except Exception as exc:  # noqa: BLE001 — детектор не должен валить аудит
        # Fail-open: при ошибке детектора считаем not-unbacked чтобы не
        # блокировать UX. Это значит при систематическом падении L4
        # фактически выключен — нужен счётчик/алерт.
        # TODO(R-39-followup): метрика detector_failure_count + админ-алерт
        # после N подряд падений (тогда L4 silently dead — это инцидент).
        logger.error(
            "post_audit: detector упал: %s: %s — пропускаем как not-unbacked",
            type(exc).__name__, exc,
        )
        is_unbacked = False

    if not is_unbacked:
        return AuditResult(
            is_unbacked=False,
            final_text=final_text,
            called_tools=called_tools,
        )

    # Сработал L4 — пишем correction state + alert
    correction_pending = _build_correction_message()
    alert_sent = False
    if admin_alert_fn is not None:
        alert_text = _build_admin_alert(
            tenant_id=tenant_id,
            turn_id=turn_id,
            called_tools=called_tools,
            final_text_preview=_preview(final_text),
        )
        try:
            admin_alert_fn(alert_text)
            alert_sent = True
        except Exception as exc:  # noqa: BLE001 — алерт не блокирует аудит
            logger.error(
                "post_audit: admin_alert упал: %s: %s",
                type(exc).__name__, exc,
            )

    return AuditResult(
        is_unbacked=True,
        final_text=final_text,
        called_tools=called_tools,
        correction_pending=correction_pending,
        alert_sent=alert_sent,
    )


# ─── Внутренние помощники ─────────────────────────────────────────────


def _is_real_call(tool_name: str) -> bool:
    """Отфильтровать generic-имена которые не должны идти в called_tools.

    Например, executor может писать FAILURE-запись с tool_name пустым
    или со специальным маркером — не считаем их вызовами для аудита.
    """
    return bool(tool_name) and not tool_name.startswith("_")


def _build_correction_message() -> str:
    """Системное сообщение для injection в next turn.

    Пишется на русском, явно — чтобы LLM поняла что нужно признать.
    """
    return (
        "В прошлом ответе ты заявила выполненное действие, но инструмент "
        "не был вызван. Начни следующий ответ с признания: «Извини, в "
        "прошлый раз я только написала, но не сделала. Сейчас сделаю.»"
    )


def _build_admin_alert(
    *,
    tenant_id: int | None,
    turn_id: str | None,
    called_tools: set[str],
    final_text_preview: str,
) -> str:
    """Сообщение для chat_id=352612382. БЕЗ user_text (152-ФЗ).

    Содержит достаточно контекста чтобы быстро найти инцидент в логах:
    tenant, turn_id, какие tools вызывались, preview ответа.
    """
    tools_list = ", ".join(sorted(called_tools)) if called_tools else "none"
    parts = [
        "🚨 R-39 L4 audit: unbacked claim detected",
        f"tenant_id={tenant_id or 'unknown'}",
        f"turn_id={turn_id or 'unknown'}",
        f"called_tools=[{tools_list}]",
        f"final_text_preview={final_text_preview!r}",
    ]
    return "\n".join(parts)


def _preview(text: str, max_len: int = 200) -> str:
    """Короткий preview без переносов — для admin alert."""
    if not text:
        return ""
    flat = " ".join(text.split())
    if len(flat) <= max_len:
        return flat
    return flat[: max_len - 1] + "…"
