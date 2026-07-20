"""Наблюдаемый исход прогона фикстуры — то, по чему судят инварианты.

Всё, что инвариант может проверить, лежит ЗДЕСЬ: реплика юзеру, признак
паузы-подтверждения, дифф состояния БД (before/after per таблица), список
tool-вызовов модели за ход, квитанции инструментов (result_kind + content),
и полная захваченная история сообщений (для ``collect_successful_writes``).

Чистые данные, без I/O — так инварианты тестируются на синтетике (RED-тесты),
не поднимая ``handle_turn``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolReceipt:
    """Квитанция одного исполненного инструмента за ход.

    ``result_kind`` — из ``ToolMessage.artifact['result_kind']`` («ok» / «error» /
    None). ``content`` — тело квитанции (префиксы ``ok:`` / ``okv2:`` / ``error:`` /
    отказ-строка). ``status`` — классификация исхода (R2, ревью sol+terra):
      * ``applied``          — доказан НОВЫЙ эффект (okv2 created непусто / success-префикс);
      * ``already_applied``  — цель уже в нужном состоянии (okv2 all-dup) — не новая строка,
                               но состояние удовлетворено;
      * ``failed``           — ошибка/отказ (result_kind!=ok / error:/отказ-строка);
      * ``noop``             — write без доказанного эффекта (не различить);
      * ``read``             — не-write инструмент."""

    name: str
    result_kind: str | None
    content: str
    status: str

    @property
    def applied(self) -> bool:
        """Эффект достигнут (новый ИЛИ уже был) — для «ответ.успех ⇒ эффект»."""
        return self.status in ("applied", "already_applied")

    @property
    def failed(self) -> bool:
        return self.status == "failed"


@dataclass
class TurnOutcome:
    """Один ход диалога: что юзер сказал, что бот ответил, что стало с БД."""

    user_text: str
    is_auto: bool                     # авто-подставленный ответ на confirm («да»/«нет»)
    reply: str                        # финальный текст юзеру (str(_Reply))
    awaiting_confirm: bool            # ход завершился паузой-подтверждением?
    db_before: dict[str, int]         # таблица -> count ДО хода
    db_after: dict[str, int]          # таблица -> count ПОСЛЕ хода
    tool_calls: list[str]             # имена инструментов, вызванных моделью за ход
    receipts: list[ToolReceipt]       # квитанции исполненных инструментов
    # R2 (ревью sol+terra): ЛОГИЧЕСКИЙ снимок состояния {таблица: {id: (status, title)}} —
    # ловит in-place мутации (archive/complete/rename), которые count не видит. Пусто в
    # синтетике инвариантов → mutated падает обратно на count-дельту.
    state_before: dict[str, Any] = field(default_factory=dict)
    state_after: dict[str, Any] = field(default_factory=dict)
    messages: list[Any] = field(default_factory=list)  # захваченная история (для collect_*)
    error: str = ""                   # аборт/краш хода (пусто = ок)

    @property
    def db_diff(self) -> dict[str, int]:
        """Ненулевые дельты count по таблицам (для expect_mutations)."""
        return {k: self.db_after[k] - self.db_before[k]
                for k in self.db_after if self.db_after[k] != self.db_before.get(k, 0)}

    @property
    def mutated(self) -> bool:
        """Ход изменил состояние БД? По логическому снимку (ловит смену status/title при
        неизменном count — archive/complete/rename), иначе по count-дельте (синтетика)."""
        if self.state_before or self.state_after:
            return self.state_before != self.state_after
        return any(v != 0 for v in self.db_diff.values())

    @property
    def has_applied_write(self) -> bool:
        """Есть доказанный write-эффект (applied/already_applied)?"""
        return any(r.applied for r in self.receipts)

    @property
    def has_failed_tool(self) -> bool:
        """Есть упавшая квитанция инструмента (status=failed)?"""
        return any(r.failed for r in self.receipts)


@dataclass
class DialogOutcome:
    """Полный прогон фикстуры: все ходы + снимок финального состояния БД."""

    fixture_id: str
    turns: list[TurnOutcome]
    final_db: dict[str, Any]          # прицельный снимок (титулы/статусы) для fixture-ассертов
    aborted: str = ""
    overrun: int = 0                  # проходов модели СВЕРХ скрипта (скрытая петля/лишние вызовы)

    @property
    def confirm_count(self) -> int:
        """Сколько ходов упёрлись в паузу-подтверждение (для потолка confirm)."""
        return sum(1 for t in self.turns if t.awaiting_confirm)

    @property
    def total_tool_calls(self) -> int:
        return sum(len(t.tool_calls) for t in self.turns)


@dataclass(frozen=True)
class Violation:
    """Нарушение инварианта. ``severity`` ∈ {CRITICAL, MAJOR, MINOR}."""

    invariant: str
    severity: str
    turn_index: int          # -1 = диалог целиком
    detail: str

    def __str__(self) -> str:  # для читаемого вывода в ассертах pytest
        where = "dialog" if self.turn_index < 0 else f"turn#{self.turn_index}"
        return f"[{self.severity}] {self.invariant} @{where}: {self.detail}"
