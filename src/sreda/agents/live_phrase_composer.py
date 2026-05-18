"""R-39: композитор живой фразы.

Второй LLM-вызов после исполнителя. Берёт уже-собранную детерминированную
первую строку и просит модель добавить ОДНУ короткую тёплую фразу.
Композитор:

- НЕ имеет доступа к инструментам (исключает класс «LLM заявила
  действие без вызова») — физическая защита.
- Имеет узкий контракт промпта (одно предложение ≤120 символов).
- Прогоняет результат через ``validate_live_phrase`` (3-правильный замок).
- При срабатывании замка возвращает пустую строку + админ-алерт
  (caller получает в ответе пользователю ТОЛЬКО первую строку — это
  и нужно: не дать confab выйти).

Чистая обёртка с DI: ``invoke_llm`` инжектится. В проде передаётся
конкретный LLM-клиент (gemini-3.1-flash-lite через ``get_chat_llm``),
в тестах — fake.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from sreda.agents.live_phrase_lock import (
    LockFail,
    LockPass,
    validate_live_phrase,
)


logger = logging.getLogger(__name__)


# Системный промпт для композитора — намеренно ограничивает простор для
# галлюцинаций. Перевод на разговорный русский + жёсткие запреты.
COMPOSER_SYSTEM_PROMPT = (
    "Ты пишешь ОДНУ короткую тёплую фразу к уже-собранному техническому "
    "ответу для пользователя.\n\n"
    "ТВОЯ ЗАДАЧА:\n"
    "Напиши одну короткую фразу (1 предложение, ≤120 символов) которая:\n"
    "— Добавляет тёплый человеческий контекст\n"
    "— Может быть с эмоцией, эмодзи, отсылкой к времени дня или контексту\n"
    "— НЕ упоминает действий («поставила», «отменила», «сохранила», "
    "«записала», «добавила» — НЕЛЬЗЯ)\n"
    "— НЕ упоминает названий/времён/имён которых нет в технической строке\n"
    "— НЕ повторяет факты — они уже сказаны технической строкой\n\n"
    "Если тёплая фраза не подходит (пользователь нейтрален) — верни пустую строку."
)


# ─── Результат композитора ───────────────────────────────────────────


@dataclass
class ComposerResult:
    """Итог компоновки живой фразы.

    ``phrase`` — то что показываем пользователю (пустая строка если
    что-то пошло не так). ``raw_phrase`` — оригинал от LLM (для лога,
    может содержать заблокированный текст). ``lock_failure`` — причина
    блокировки замком (для админ-алертов).
    """

    phrase: str
    raw_phrase: str = ""
    lock_failure: LockFail | None = None


InvokeLLMFn = Callable[[str, str], str | None]
AdminAlertFn = Callable[[str], None]


# ─── Главная функция ──────────────────────────────────────────────────


def compose_live_phrase(
    *,
    user_text: str,
    first_line: str,
    journal_summary: str = "",
    invoke_llm: InvokeLLMFn,
    admin_alert_fn: AdminAlertFn | None = None,
    max_length: int = 120,
) -> ComposerResult:
    """Сгенерировать живую фразу или вернуть пустую при сбое/отказе замка.

    Args:
        user_text: исходное сообщение пользователя.
        first_line: уже-собранная детерминированная первая строка
            подтверждения. Все факты в ней — единственный источник истины.
        journal_summary: опциональное краткое описание журнала действий
            (для контекста, не для прямой подстановки).
        invoke_llm: callable вызова LLM ``(system, user) -> str | None``.
            Возвращает ``None`` при таймауте/ошибке.
        admin_alert_fn: опциональный callback для алертов админу когда
            замок сработал (сигнал что LLM пыталась confab'нуть).
        max_length: лимит длины фразы (передаётся в замок).

    Returns:
        ``ComposerResult`` с финальной (прошедшей замок) фразой или
        пустой строкой при любой проблеме.
    """
    user_prompt = _build_user_prompt(
        user_text=user_text,
        first_line=first_line,
        journal_summary=journal_summary,
    )

    raw: str | None
    try:
        raw = invoke_llm(COMPOSER_SYSTEM_PROMPT, user_prompt)
    except Exception as exc:  # noqa: BLE001 — таймаут/сеть/HTTP
        logger.warning(
            "live_phrase_composer: invoke_llm бросил %s — пустая фраза",
            type(exc).__name__,
        )
        return ComposerResult(phrase="", raw_phrase="")

    if raw is None:
        return ComposerResult(phrase="", raw_phrase="")

    raw_stripped = raw.strip()
    if not raw_stripped:
        return ComposerResult(phrase="", raw_phrase="")

    # Замок
    verdict = validate_live_phrase(
        raw_stripped, first_line, max_length=max_length
    )
    if isinstance(verdict, LockPass):
        return ComposerResult(phrase=raw_stripped, raw_phrase=raw_stripped)

    # LockFail — гасим живую фразу + админ-алерт
    assert isinstance(verdict, LockFail)
    logger.warning(
        "live_phrase_composer: замок отверг фразу reason=%s detail=%s",
        verdict.reason, verdict.detail,
    )
    if admin_alert_fn is not None:
        try:
            admin_alert_fn(
                f"R-39 живая фраза отвергнута замком: {verdict.reason}/"
                f"{verdict.detail}; first_line={first_line[:80]!r}"
            )
        except Exception as exc:  # noqa: BLE001 — алерт не должен валить ход
            logger.error(
                "live_phrase_composer: admin_alert_fn упал: %s", exc
            )

    return ComposerResult(
        phrase="",
        raw_phrase=raw_stripped,
        lock_failure=verdict,
    )


# ─── Внутренние помощники ─────────────────────────────────────────────


def _build_user_prompt(
    *,
    user_text: str,
    first_line: str,
    journal_summary: str,
) -> str:
    """Сборка user-промпта с контекстом для композитора."""
    parts = [
        f"Запрос пользователя: «{user_text}»",
        "",
        "Технический ответ, который пользователь УЖЕ увидит первой строкой:",
        f"«{first_line}»",
    ]
    if journal_summary:
        parts.extend(["", "Журнал сделанного:", journal_summary])
    parts.extend(["", "Твоя ОДНА короткая фраза:"])
    return "\n".join(parts)
