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
    отказ-строка). ``applied`` — доказанный эффект (ok-статус И не отказ/ошибка)."""

    name: str
    result_kind: str | None
    content: str
    applied: bool


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
    messages: list[Any] = field(default_factory=list)  # захваченная история (для collect_*)
    error: str = ""                   # аборт/краш хода (пусто = ок)

    @property
    def db_diff(self) -> dict[str, int]:
        """Ненулевые дельты count по таблицам (мутации этого хода)."""
        return {k: self.db_after[k] - self.db_before[k]
                for k in self.db_after if self.db_after[k] != self.db_before.get(k, 0)}

    @property
    def mutated(self) -> bool:
        """Ход изменил БД (любая таблица выросла/уменьшилась)?"""
        return any(v != 0 for v in self.db_diff.values())

    @property
    def has_applied_write(self) -> bool:
        """Есть доказанный успешный write-эффект (квитанция applied)?"""
        return any(r.applied for r in self.receipts)

    @property
    def has_failed_tool(self) -> bool:
        """Есть квитанция инструмента с ошибкой (result_kind != ok)?"""
        return any(r.result_kind not in (None, "ok") for r in self.receipts)


@dataclass
class DialogOutcome:
    """Полный прогон фикстуры: все ходы + снимок финального состояния БД."""

    fixture_id: str
    turns: list[TurnOutcome]
    final_db: dict[str, Any]          # прицельный снимок (титулы/статусы) для fixture-ассертов
    aborted: str = ""

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
