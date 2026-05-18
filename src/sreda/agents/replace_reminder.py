"""R-39: атомарная замена напоминания (Codex CRITICAL).

Кати-сценарий: пользователь сказал «нет, не на 2 а на 14, разбудить
Катю». Без атомарности это два отдельных шага: cancel-old, schedule-new.
Если schedule-new упал, старое уже отменено — образуется состояние
«нет ничего», хотя пользователь ждёт замену.

``atomic_replace_reminder`` инкапсулирует обе операции и явно ловит
этот случай:

- **SUCCESS** — оба шага прошли, в БД новое напоминание.
- **PARTIAL_ONLY_CANCELED** — старое отменено, новое не создано. Если
  передан ``rollback_cancel_fn``, попытка восстановить отменённое.
  Без него — журнал PARTIAL + понятная ошибка пользователю.
- **PARTIAL_INCONSISTENT** — старое отменено + новое не создалось +
  rollback тоже упал. Самый тяжёлый случай, требует ручного вмешательства.
- **TOTAL_FAILURE** — cancel сразу упал, в БД ничего не менялось.

Функция чистая: всё реальное общение с БД через ``cancel_fn``,
``create_fn``, ``rollback_cancel_fn`` (DI). Это позволяет
тестировать ветви без поднятия сессии и интегрировать в
``housewife_chat_tools`` в день 4.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


logger = logging.getLogger(__name__)


class ReplaceOutcome(str, Enum):
    """Возможный итог атомарной замены."""

    SUCCESS = "success"
    PARTIAL_ONLY_CANCELED = "partial_only_canceled"     # cancel OK, create FAIL
    PARTIAL_INCONSISTENT = "partial_inconsistent"       # cancel OK + create FAIL + rollback FAIL
    TOTAL_FAILURE = "total_failure"                     # cancel FAIL — ничего не менялось
    # PARTIAL_ONLY_CREATED намеренно отсутствует: cancel идёт первым и
    # синхронно — если он упал, мы не вызываем create. Сценарий
    # «create OK + cancel FAIL» возможен только при гонке двух
    # процессов; если такая гонка появится — добавить вариант + код.


@dataclass
class ReplaceResult:
    """Итог атомарной замены."""

    outcome: ReplaceOutcome
    new_reminder_id: str | None = None        # id созданного (если был)
    cancelled_reminder_id: str | None = None  # id отменённого (если был)
    rolled_back: bool = False                 # удалось ли откатить cancel
    error: str | None = None                  # текст последней ошибки


CancelFn = Callable[[str], Any]
CreateFn = Callable[[str, str], Any]
RollbackFn = Callable[[str], Any]


# ─── Главная функция ──────────────────────────────────────────────────


def atomic_replace_reminder(
    old_reminder_id: str,
    new_title: str,
    new_trigger_iso: str,
    *,
    cancel_fn: CancelFn,
    create_fn: CreateFn,
    rollback_cancel_fn: RollbackFn | None = None,
    extract_new_id: Callable[[Any], str | None] | None = None,
) -> ReplaceResult:
    """Атомарно отменить старое напоминание и создать новое.

    Args:
        old_reminder_id: идентификатор отменяемого напоминания.
        new_title: заголовок нового напоминания.
        new_trigger_iso: ISO время нового напоминания.
        cancel_fn: реальная функция отмены ``(reminder_id) -> None``.
        create_fn: реальная функция создания
            ``(title, trigger_iso) -> created (any)``.
        rollback_cancel_fn: опциональная функция восстановления
            отменённого напоминания (если БД позволяет).
        extract_new_id: опциональный извлекатель id из результата
            ``create_fn``. По умолчанию пытаемся прочесть атрибут или
            ключ ``reminder_id``.

    Returns:
        ``ReplaceResult`` с явным описанием итога.
    """
    extract_new_id = extract_new_id or _default_extract_id

    # Шаг 1: cancel
    try:
        cancel_fn(old_reminder_id)
    except Exception as exc:  # noqa: BLE001 — любая ошибка cancel
        logger.warning(
            "replace_reminder: cancel упал old_id=%s: %s",
            old_reminder_id, exc,
        )
        return ReplaceResult(
            outcome=ReplaceOutcome.TOTAL_FAILURE,
            error=f"cancel_failed: {type(exc).__name__}: {exc}",
        )

    # Шаг 2: create
    try:
        created = create_fn(new_title, new_trigger_iso)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "replace_reminder: create упал после успешного cancel old_id=%s: %s",
            old_reminder_id, exc,
        )
        # Попытка отката cancel
        if rollback_cancel_fn is not None:
            try:
                rollback_cancel_fn(old_reminder_id)
            except Exception as roll_exc:  # noqa: BLE001
                logger.error(
                    "replace_reminder: rollback тоже упал old_id=%s: %s",
                    old_reminder_id, roll_exc,
                )
                return ReplaceResult(
                    outcome=ReplaceOutcome.PARTIAL_INCONSISTENT,
                    cancelled_reminder_id=old_reminder_id,
                    rolled_back=False,
                    error=(
                        f"create_failed_and_rollback_failed: "
                        f"create={type(exc).__name__}({exc}); "
                        f"rollback={type(roll_exc).__name__}({roll_exc})"
                    ),
                )
            # rollback succeeded — пользователь видит старое восстановленным,
            # но желаемое новое так и не создалось. Это партиал — caller
            # должен явно сообщить «не получилось обновить, оставила как было».
            return ReplaceResult(
                outcome=ReplaceOutcome.PARTIAL_ONLY_CANCELED,
                cancelled_reminder_id=old_reminder_id,
                rolled_back=True,
                error=f"create_failed_rolled_back: {type(exc).__name__}: {exc}",
            )
        # Без rollback — фиксируем data loss риск явно
        return ReplaceResult(
            outcome=ReplaceOutcome.PARTIAL_ONLY_CANCELED,
            cancelled_reminder_id=old_reminder_id,
            rolled_back=False,
            error=f"create_failed_no_rollback: {type(exc).__name__}: {exc}",
        )

    # Оба шага прошли
    new_id = extract_new_id(created)
    return ReplaceResult(
        outcome=ReplaceOutcome.SUCCESS,
        cancelled_reminder_id=old_reminder_id,
        new_reminder_id=new_id,
    )


# ─── Внутренние помощники ─────────────────────────────────────────────


def _default_extract_id(created: Any) -> str | None:
    """Достать reminder_id из словаря или объекта результата create."""
    if created is None:
        return None
    if isinstance(created, dict):
        value = created.get("reminder_id") or created.get("id")
        return str(value) if value is not None else None
    # ORM-объект или dataclass
    for attr in ("reminder_id", "id"):
        if hasattr(created, attr):
            value = getattr(created, attr)
            if value is not None:
                return str(value)
    return None
