"""R-39: контракты инструментов для гибридного полу-шаблонного потока.

Каждый пишущий инструмент имеет ``ToolContract`` — декларативное
описание: что делает, какие поля обязательны, как считать идемпотентный
ключ и какие варианты первой строки подставлять при успехе/ошибке.

``ToolJournalEntry`` — запись в журнале хода. Заполняется исполнителем
после реального вызова инструмента. ``result_data`` уже нормализована
для прямой подстановки в шаблоны (``trigger_human`` посчитан
``date_formatter``, ``title`` пропущен через ``sanitize_for_display``).

``TurnContext`` — стабильные значения для отрисовки одной первой строки
(``turn_id`` определяет seed выбора варианта шаблона).

Реестр ``TOOL_CONTRACTS`` начинается с 6 ключевых инструментов; план
требует постепенного расширения до 49 (день 3-4). Каждый отсутствующий
в реестре mutating-инструмент будет ловиться startup-assert (готовится
в день 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ─── Перечисления ─────────────────────────────────────────────────────


class MutationKind(str, Enum):
    """Природа действия инструмента."""

    WRITE = "write"        # создание новой сущности
    DELETE = "delete"      # удаление существующей
    REPLACE = "replace"    # атомарная замена (cancel+create в одной операции)
    READ = "read"          # только чтение (для генерик-fallback)


class IdempotencyStrategy(str, Enum):
    """Как считать идемпотентный ключ.

    - ``per_turn`` — один и тот же ход не должен повторять действие
      (parallel hedge / retry). Ключ = (tenant, turn_id, tool, action_index).
    - ``per_entity`` — повторное действие над тем же `entity_id`
      идемпотентно. Ключ = (tenant, tool, entity_id).
    - ``natural_key`` — ключ строится из ``natural_key_fields`` (например
      ``title + trigger_iso`` для напоминания). Защита от дубликатов
      «поставь напоминалку на 14:00» подряд двумя сообщениями.
    """

    PER_TURN = "per_turn"
    PER_ENTITY = "per_entity"
    NATURAL_KEY = "natural_key"


class ResultKind(str, Enum):
    """Полярность исхода действия — определяет какие шаблоны брать."""

    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"   # действие частично сработало


# ─── Контракт инструмента ─────────────────────────────────────────────


@dataclass(frozen=True)
class ToolContract:
    """Декларативное описание пишущего инструмента.

    Поля для startup-assert (день 5):
      - ``success_template_variants`` непустой (≥3 варианта)
      - ``failure_template_variants`` непустой (≥1 вариант)
      - ``partial_template_variants`` непустой ТОЛЬКО если
        ``supports_partial=True`` (защита от шаблонов которые никогда
        не отрендерятся)
      - все ``required_fields`` упоминаются хотя бы в одном варианте
        ``success_template_variants`` как ``{name}`` (защита от шаблонов
        пропускающих ключевое поле)
      - при ``idempotency_strategy=NATURAL_KEY`` поле
        ``natural_key_fields`` непустое
    """

    mutating: bool
    mutation_kind: MutationKind
    action_type: str                          # короткий ярлык для журнала
    required_fields: dict[str, str]           # name → type-hint строкой

    idempotency_strategy: IdempotencyStrategy
    entity_id_field: str | None = None        # имя поля содержащего id сущности (для PER_ENTITY)
    natural_key_fields: tuple[str, ...] = ()  # для NATURAL_KEY (Codex R6 #7)

    # Полу-шаблон: варианты первой строки
    success_template_variants: tuple[str, ...] = ()
    failure_template_variants: tuple[str, ...] = ()
    partial_template_variants: tuple[str, ...] = ()
    supports_partial: bool = False            # отдельный флаг — даёт явный сигнал что partial возможен


# ─── Запланированный вызов и план хода ────────────────────────────────


@dataclass(frozen=True)
class ToolCall:
    """Один запланированный вызов инструмента.

    Выпускается планировщиком (или формируется детерминированной
    логикой при коррекции) и передаётся исполнителю. После исполнения
    превращается в ``ToolJournalEntry``.

    Внимание: ``args`` — обычный ``dict`` (frozen-датакласс не делает
    его иммутабельным). Не мутировать значения после создания вызова —
    исполнитель полагается на стабильность аргументов между расчётом
    idempotency-ключа и фактическим вызовом callable. Если planner-у
    нужна модификация — создавать новый ``ToolCall``.
    """

    tool_name: str
    args: dict[str, Any]
    action_index: int   # 0-based позиция в плане


@dataclass(frozen=True)
class ExecutionPlan:
    """План действий хода — упорядоченный список вызовов.

    Пустой план = NoAction (планировщик решил что mutation не нужен).
    Один вызов = типичный mutation. Несколько = редкий cancel+schedule
    или multi-action ход.
    """

    calls: tuple[ToolCall, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.calls

    def __len__(self) -> int:
        return len(self.calls)


# ─── Альтернативные исходы планировщика ──────────────────────────────


@dataclass(frozen=True)
class NoAction:
    """Планировщик решил: инструмент не нужен.

    Сообщение ``ack_message`` — короткая нейтральная фраза для пользователя
    («Поняла», «Хорошо», «Спасибо»). Не используется как первая строка
    подтверждения — отправляется как полная реплика бота. Опциональное
    ``rationale`` пишется в лог для отладки.
    """

    ack_message: str
    rationale: str = ""


@dataclass(frozen=True)
class Clarification:
    """Планировщик не уверен в действии — спрашиваем у пользователя.

    Сценарий: «на 2 часа» без квалификатора (ambiguous time), «отмени»
    без указания какого именно напоминания (несколько pending), и т.п.
    ``question`` отправляется пользователю как короткое уточнение.
    """

    question: str
    rationale: str = ""


# Союзный тип итога планировщика определён в sreda.agents.planner как
# реальный UnionType (Python 3.10+). Здесь не дублируем чтобы не было
# двух разных объектов с одним именем.


# ─── Запись журнала ───────────────────────────────────────────────────


@dataclass
class ToolJournalEntry:
    """Одно действие в журнале хода.

    Исполнитель добавляет запись после фактического вызова инструмента,
    с уже нормализованным ``result_data`` (sanitize_for_display + date_formatter).
    """

    tool_name: str
    action_index: int                         # 0-based порядковый номер в обороте
    result_kind: ResultKind
    result_data: dict[str, Any]               # для подстановки в шаблон
    error_message: str | None = None          # текст ошибки если FAILURE/PARTIAL
    entity_id: str | None = None              # id созданной/обновлённой сущности
    idempotency_key: str | None = None        # для аудита и повтора


# ─── История разговора ───────────────────────────────────────────────


@dataclass(frozen=True)
class ConversationTurn:
    """Одна реплика разговора в истории.

    Хранится в обороте контекста (последние 3-5 ходов) для коррекций
    и multi-turn разрешения. ``journal_entries`` — что бот сделал в
    ответе на этот пользовательский ход; пустой список = no-action.

    ``turn_id`` нужен ``correction_resolver`` чтобы при необходимости
    сослаться на конкретный ход; ``timestamp_utc`` — для отбора по
    свежести (свежее = вероятнее target).
    """

    user_text: str
    journal_entries: tuple[ToolJournalEntry, ...]
    turn_id: str
    timestamp_utc: str  # ISO datetime UTC (строкой — frozen-friendly)


# ─── Контекст одного хода ─────────────────────────────────────────────


@dataclass(frozen=True)
class TurnContext:
    """Стабильные значения хода — используются для seed-а выбора варианта.

    ``turn_id`` остаётся постоянным в пределах одного хода — даже если
    рендер вызвали повторно (retry, hedge), та же первая строка
    воспроизводится. Без этой стабильности parallel hedge мог бы
    показать пользователю две разные подтверждающие фразы.
    """

    turn_id: str
    tenant_id: int
    user_tz: str = "Europe/Moscow"


# ─── Реестр ключевых контрактов ───────────────────────────────────────


_SCHEDULE_REMINDER = ToolContract(
    mutating=True,
    mutation_kind=MutationKind.WRITE,
    action_type="create_reminder",
    required_fields={
        "title": "string",
        "trigger_iso": "datetime",
    },
    idempotency_strategy=IdempotencyStrategy.NATURAL_KEY,
    natural_key_fields=("title", "trigger_iso"),
    success_template_variants=(
        "Готово ✅ «{title}» — {trigger_human} ⏰",
        "Поставила ⏰ «{title}» на {trigger_human}",
        "Записала: «{title}» — {trigger_human} 📋",
        "На {trigger_human} — «{title}» ⏰",
        "Запланировала «{title}» на {trigger_human} ⏰",
        "Хорошо, «{title}» — {trigger_human}",
        "Принято — «{title}», {trigger_human}",
    ),
    failure_template_variants=(
        "Не получилось поставить напоминание. Попробуй ещё раз — например «напомни в 14:00 разбудить Катю».",
        "Что-то не сработало с напоминанием. Скажи попробовать ещё раз — например «напомни вечером купить хлеб».",
    ),
    partial_template_variants=(),
    supports_partial=False,
)


_CANCEL_REMINDER = ToolContract(
    mutating=True,
    mutation_kind=MutationKind.DELETE,
    action_type="cancel_reminder",
    required_fields={"reminder_id": "string"},
    idempotency_strategy=IdempotencyStrategy.PER_ENTITY,
    entity_id_field="reminder_id",
    success_template_variants=(
        "Отменила «{title}» ✕",
        "Готово — сняла «{title}»",
        "Убрала «{title}» ✕",
        "Отменено: «{title}»",
        "Сняла напоминание «{title}»",
    ),
    failure_template_variants=(
        "Не получилось отменить — возможно, уже снято. Проверь список напоминаний.",
        "Не нашла такого напоминания. Скажи точнее — например «отмени напоминание про врача».",
    ),
)


_REPLACE_REMINDER = ToolContract(
    mutating=True,
    mutation_kind=MutationKind.REPLACE,
    action_type="replace_reminder",
    required_fields={
        "reminder_id": "string",
        "title": "string",
        "trigger_iso": "datetime",
    },
    idempotency_strategy=IdempotencyStrategy.PER_ENTITY,
    entity_id_field="reminder_id",
    success_template_variants=(
        "Поправила — теперь «{title}» на {trigger_human} ⏰",
        "Перенесла «{title}» — теперь {trigger_human}",
        "Обновила: «{title}» — {trigger_human}",
        "Готово, «{title}» — на {trigger_human}",
    ),
    failure_template_variants=(
        "Не получилось обновить напоминание. Попробуй заново — отменю старое и поставлю новое.",
        "Что-то не сработало с заменой. Скажи ещё раз — например «перенеси на 14:00 разбудить Катю».",
    ),
    partial_template_variants=(
        "Старое снято, но новое не получилось поставить — попробуй сказать ещё раз.",
        "Новое поставила, но старое не снялось — проверь список напоминаний.",
    ),
    supports_partial=True,
)


_SAVE_RECIPE = ToolContract(
    mutating=True,
    mutation_kind=MutationKind.WRITE,
    action_type="save_recipe",
    required_fields={"title": "string"},
    idempotency_strategy=IdempotencyStrategy.NATURAL_KEY,
    natural_key_fields=("title",),
    success_template_variants=(
        "Сохранила рецепт «{title}» 📖",
        "Записала рецепт «{title}» 📖",
        "Готово — «{title}» в книге 📖",
        "Добавила «{title}» в рецепты",
        "В книге: «{title}» 📖",
    ),
    failure_template_variants=(
        "Не получилось сохранить рецепт. Скажи ещё раз — например «запиши рецепт борща».",
        "Что-то не сработало с рецептом. Попробуй заново.",
    ),
)


_ADD_SHOPPING_ITEMS = ToolContract(
    mutating=True,
    mutation_kind=MutationKind.WRITE,
    action_type="add_shopping_items",
    required_fields={"items_summary": "string"},
    idempotency_strategy=IdempotencyStrategy.PER_TURN,
    success_template_variants=(
        "Добавила в список: {items_summary} 🛒",
        "В список покупок — {items_summary} 🛒",
        "Записала: {items_summary} 🛒",
        "Готово — {items_summary} в списке",
        "Положила в список {items_summary}",
    ),
    failure_template_variants=(
        "Не получилось добавить в список. Скажи ещё раз — например «добавь молоко и хлеб».",
        "Что-то не сработало со списком покупок. Попробуй заново.",
    ),
    partial_template_variants=(
        "Часть добавила, но {what_failed} — проверь список.",
    ),
    supports_partial=True,
)


_COMPLETE_TASK = ToolContract(
    mutating=True,
    mutation_kind=MutationKind.WRITE,
    action_type="complete_task",
    required_fields={"title": "string", "task_id": "string"},
    idempotency_strategy=IdempotencyStrategy.PER_ENTITY,
    entity_id_field="task_id",
    success_template_variants=(
        "Отметила «{title}» как сделано ✓",
        "Готово — «{title}» ✓",
        "Закрыла «{title}» ✓",
        "Поставила галочку: «{title}» ✓",
        "«{title}» — выполнено ✓",
    ),
    failure_template_variants=(
        "Не получилось отметить задачу. Скажи точнее — например «отметь что я погулял с собакой».",
        "Не нашла такую задачу — может другая формулировка?",
    ),
)


TOOL_CONTRACTS: dict[str, ToolContract] = {
    "schedule_reminder": _SCHEDULE_REMINDER,
    "cancel_reminder": _CANCEL_REMINDER,
    "replace_reminder": _REPLACE_REMINDER,
    "save_recipe": _SAVE_RECIPE,
    "add_shopping_items": _ADD_SHOPPING_ITEMS,
    "complete_task": _COMPLETE_TASK,
}


# ─── Стартап-проверки ─────────────────────────────────────────────────


class ContractError(Exception):
    """Контракт инструмента не проходит инвариант — система не должна стартовать."""


def assert_contracts_well_formed(
    contracts: dict[str, ToolContract] | None = None,
    *,
    min_success_variants: int = 3,
) -> None:
    """Стартап-инвариант: каждый контракт удовлетворяет требованиям.

    Падает с ``ContractError`` если:
      - success_template_variants короче ``min_success_variants``
      - failure_template_variants пуст
      - supports_partial=True, но partial_template_variants пуст
      - supports_partial=False, но partial_template_variants непуст (мёртвые шаблоны)
      - в success-вариантах нет упоминания ВСЕХ required_fields
      - idempotency=NATURAL_KEY без natural_key_fields
      - idempotency=PER_ENTITY без entity_id_field
    """
    contracts = contracts if contracts is not None else TOOL_CONTRACTS
    errors: list[str] = []

    for name, contract in contracts.items():
        if len(contract.success_template_variants) < min_success_variants:
            errors.append(
                f"{name}: success_template_variants короче {min_success_variants} "
                f"({len(contract.success_template_variants)})"
            )
        if not contract.failure_template_variants:
            errors.append(f"{name}: failure_template_variants пуст")
        if contract.supports_partial and not contract.partial_template_variants:
            errors.append(
                f"{name}: supports_partial=True но partial_template_variants пуст"
            )
        if not contract.supports_partial and contract.partial_template_variants:
            errors.append(
                f"{name}: supports_partial=False но partial_template_variants "
                f"непуст ({len(contract.partial_template_variants)}) — мёртвые шаблоны"
            )

        # required_fields должны встречаться хотя бы в одном success-шаблоне
        all_success = " ".join(contract.success_template_variants)
        for field_name in contract.required_fields:
            placeholder = "{" + field_name + "}"
            # допускаем что поле упомянуто через производное (trigger_iso → trigger_human)
            if field_name == "trigger_iso":
                placeholder_alt = "{trigger_human}"
                if placeholder not in all_success and placeholder_alt not in all_success:
                    errors.append(
                        f"{name}: ни один success-вариант не использует {{trigger_iso}} "
                        f"или {{trigger_human}} (производное)"
                    )
                continue
            if field_name == "task_id":
                continue  # task_id обычно для backend, не для пользователя
            if field_name == "reminder_id":
                continue  # тоже backend-id
            if placeholder not in all_success:
                errors.append(
                    f"{name}: ни один success-вариант не использует "
                    f"плейсхолдер {placeholder}"
                )

        if contract.idempotency_strategy is IdempotencyStrategy.NATURAL_KEY:
            if not contract.natural_key_fields:
                errors.append(
                    f"{name}: idempotency=NATURAL_KEY требует natural_key_fields"
                )
            # natural_key_fields должны быть в required_fields
            for nk in contract.natural_key_fields:
                if nk not in contract.required_fields:
                    errors.append(
                        f"{name}: natural_key_fields содержит {nk!r}, "
                        f"но в required_fields его нет"
                    )
        if contract.idempotency_strategy is IdempotencyStrategy.PER_ENTITY:
            if not contract.entity_id_field:
                errors.append(
                    f"{name}: idempotency=PER_ENTITY требует entity_id_field"
                )
            # entity_id_field должен быть в required_fields — иначе executor
            # подставит None и все вызовы свернутся в один idempotency-ключ
            elif contract.entity_id_field not in contract.required_fields:
                errors.append(
                    f"{name}: entity_id_field={contract.entity_id_field!r} "
                    f"должно быть в required_fields"
                )

    if errors:
        msg = "Контракты инструментов нарушают инвариант:\n  - " + "\n  - ".join(errors)
        raise ContractError(msg)
