"""Чек-листы — третий тип «списков» в продукте.

Концептуально:
  * ``ShoppingListItem`` — продукты в магазине (есть категория,
    единица измерения, у юзера один глобальный shopping-список).
  * ``Task`` — события с датой и временем (попадают в Расписание,
    могут быть recurring, привязаны к FamilyReminder).
  * ``Checklist`` + ``ChecklistItem`` — произвольный именованный
    список дел со статусом сделано/не сделано, без привязки к
    дате. У юзера может быть несколько активных списков
    («План кроя», «Дела на дачу», «Подготовка к школе»).

Главный кейс: швея/мастер диктует план работ голосом, бот собирает
именованный список с пунктами, потом юзер по голосу отмечает
«закройила лаванду» — пункт получает status=done, в Mini App
рисуется ☑ + strikethrough.

EncryptedString для title/notes пунктов — содержимое (размеры,
материалы, назначения) считаем PII-эквивалентом. title самого
checklist — обычная String (название «План кроя» — не sensitive).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base
from sreda.db.types import EncryptedString


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


CHECKLIST_STATUSES = ("active", "archived")
CHECKLIST_ITEM_STATUSES = ("pending", "done", "cancelled")


class Checklist(Base):
    """Именованный список дел.

    Жизненный цикл:
      * ``create_checklist`` (LLM tool) → status='active'.
      * ``add_checklist_items`` → ChecklistItem'ы внутри.
      * ``mark_checklist_item_done`` → пункт done; checklist остаётся.
      * ``archive_checklist`` → status='archived'; не показывается
        в Mini App / list_checklists.

    Title (название самого списка) — String, не EncryptedString:
    «План кроя на эту неделю», «Дела на дачу» — не sensitive.
    Контент пунктов (что именно резать) — sensitive, в
    ChecklistItem.title через EncryptedString.
    """

    __tablename__ = "checklists"
    __table_args__ = (
        Index(
            "ix_checklists_active",
            "tenant_id", "user_id", "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id"), nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id"), nullable=False,
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )


class ChecklistItem(Base):
    """Пункт чек-листа.

    Содержимое (title/notes) шифруется EncryptedString — это сродни
    Task: «Лаванда 298 ТС, простыня 141×200×19» — деталь работы юзера,
    которую не хотим читать в plaintext дампах.

    position — сортировка пунктов внутри checklist (рендер в Mini App
    идёт в этом порядке; новые добавляются в конец).
    """

    __tablename__ = "checklist_items"
    __table_args__ = (
        Index(
            "ix_checklist_items_status",
            "checklist_id", "status", "position",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    checklist_id: Mapped[str] = mapped_column(
        ForeignKey("checklists.id", ondelete="CASCADE"), nullable=False,
    )

    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    title: Mapped[str] = mapped_column(EncryptedString(), nullable=False)
    notes: Mapped[str | None] = mapped_column(EncryptedString(), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending",
    )
    done_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    # Embedding for recall-broadcast (migration 0037, Phase 1).
    # Generated from f"{checklist.title}: {item.title}" via e5-large
    # passage prefix. NULL for legacy rows until backfill runs.
    # ``embedding_dim`` not stored — derivable from ``embedding_model``.
    #
    # SECURITY tradeoff: ``title`` shifrуется EncryptedString, а
    # ``embedding_json`` хранится plaintext. Атакующий с DB read-доступом
    # может через cosine similarity сделать guessing attack — частично
    # обходит encryption-at-rest. Принят как accepted risk (тот же
    # паттерн в AssistantMemory.embedding_json), потому что:
    #   1) cosine over плейн-эмбеддингов нужен для recall;
    #   2) шифрование embedding_json потребовало бы decrypt-on-recall,
    #      что фундаментально несовместимо с in-place vector search.
    # Если threat-model изменится — мигрировать обе таблицы вместе.
    embedding_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
