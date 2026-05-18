"""R-39: журнал действий одного хода.

``ToolJournal`` — контейнер ``ToolJournalEntry``. Содержит дедупликацию
по ``idempotency_key`` (для случая parallel hedge / retry — второй вызов
видит запись первого и отвергается без дубликата в журнале).

Используется и исполнителем (накапливает результаты), и рендерером
первой строки (читает успешные записи для формирования подтверждения),
и пост-фактум-аудитом (проверяет соответствие журнала и
тексту-подтверждения).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from sreda.agents.contracts import ResultKind, ToolJournalEntry


@dataclass
class ToolJournal:
    """Журнал одного хода — упорядоченный список записей с dedup."""

    entries: list[ToolJournalEntry] = field(default_factory=list)
    _key_index: dict[str, ToolJournalEntry] = field(default_factory=dict, repr=False)

    # ─── Mutation ─────────────────────────────────────────────────────

    def append(self, entry: ToolJournalEntry) -> bool:
        """Добавить запись. Возвращает True если добавлена, False если дубль.

        Дедупликация если у записи задан ``idempotency_key`` (явная
        проверка ``is not None``, а не truthy — защита от случайного
        пустого ключа). Записи с ``idempotency_key=None`` (read-tools,
        generic ack, unknown_tool failure) добавляются без проверки.
        """
        if entry.idempotency_key is not None:
            if entry.idempotency_key in self._key_index:
                return False
            self._key_index[entry.idempotency_key] = entry
        self.entries.append(entry)
        return True

    # ─── Запросы ──────────────────────────────────────────────────────

    def has_key(self, idempotency_key: str) -> bool:
        """Есть ли уже запись с этим idempotency_key? O(1)."""
        return idempotency_key in self._key_index

    def find_by_key(self, idempotency_key: str) -> ToolJournalEntry | None:
        """Найти запись по idempotency_key (None если нет). O(1)."""
        return self._key_index.get(idempotency_key)

    @property
    def is_empty(self) -> bool:
        return not self.entries

    @property
    def has_failures(self) -> bool:
        """Хотя бы одна запись — FAILURE или PARTIAL?"""
        return any(
            e.result_kind in (ResultKind.FAILURE, ResultKind.PARTIAL)
            for e in self.entries
        )

    @property
    def all_succeeded(self) -> bool:
        """Все записи — SUCCESS?"""
        if not self.entries:
            return False
        return all(e.result_kind is ResultKind.SUCCESS for e in self.entries)

    @property
    def overall_kind(self) -> ResultKind:
        """Сводный итог журнала для рендера partial-варианта.

        - Пустой журнал → FAILURE (нечего рендерить как успех).
        - Только SUCCESS → SUCCESS.
        - Только FAILURE → FAILURE.
        - Смесь SUCCESS+FAILURE → PARTIAL (multi-step: что-то прошло,
          что-то нет → должен сработать partial-шаблон в renderer).
        - Есть PARTIAL запись → PARTIAL.
        """
        if not self.entries:
            return ResultKind.FAILURE
        if any(e.result_kind is ResultKind.PARTIAL for e in self.entries):
            return ResultKind.PARTIAL
        has_success = any(e.result_kind is ResultKind.SUCCESS for e in self.entries)
        has_failure = any(e.result_kind is ResultKind.FAILURE for e in self.entries)
        if has_success and has_failure:
            return ResultKind.PARTIAL
        if has_success:
            return ResultKind.SUCCESS
        return ResultKind.FAILURE

    @property
    def successful(self) -> list[ToolJournalEntry]:
        """Только успешные записи (для рендера подтверждения)."""
        return [e for e in self.entries if e.result_kind is ResultKind.SUCCESS]

    @property
    def failed(self) -> list[ToolJournalEntry]:
        """Только проваленные записи (для отчёта об ошибке)."""
        return [e for e in self.entries if e.result_kind is ResultKind.FAILURE]

    # ─── Удобства ─────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[ToolJournalEntry]:
        return iter(self.entries)
