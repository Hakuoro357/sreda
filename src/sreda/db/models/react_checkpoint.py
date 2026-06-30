"""#193: durable персистентность диалога ReAct (checkpoint-saver, вариант B).

Две таблицы под кастомный `EncryptedSqlCheckpointSaver` (runtime/react_checkpoint_saver.py):

- ``react_checkpoint``: один checkpoint = одна строка. ВЕСЬ checkpoint (incl ``channel_values`` —
  там messages/контент) сериализован штатным ``EncryptedSerializer`` (``serde.dumps_typed`` →
  ``(type, ciphertext)``) → ``blob`` (BYTEA) + ``checkpoint_type``. channel_values НЕ выносим в
  отдельную version-keyed blob-таблицу (как делают InMemory/Postgres savers) — 1-blob inline: проще
  протокол, цена — полное состояние в каждой строке (ограничено GC 30д; диалоги короткие).
- ``react_checkpoint_write``: pending/interrupt writes. ``idx`` ЗНАКОВЫЙ — спец-writes отрицательные
  (INTERRUPT=-3 / RESUME=-4 / ERROR=-1 / SCHEDULED=-2, ``WRITES_IDX_MAP``), БЕЗ CHECK≥0.

ПД: ``blob``/``metadata`` = ciphertext (шифрует EncryptedSerializer на уровне serde, НЕ колонка) →
``LargeBinary`` без type-decorator. ``thread_id`` уже хеширован (``react:{tenant}:{hmac(chat)}``) в
react_loop. GC 30д — по last-activity треда (cleanup_runtime_retention).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


class ReactCheckpoint(Base):
    __tablename__ = "react_checkpoint"

    thread_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(String(64), primary_key=True, default="")
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_checkpoint_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # (type, ciphertext) из serde.dumps_typed — нужны ОБА для loads_typed. EncryptedSerializer пишет
    # type как "{serde_type}+{key_id}" → ширина с запасом под длинный key_id (CR R1 MINOR Codex high).
    checkpoint_type: Mapped[str] = mapped_column(String(255), nullable=False)
    blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    metadata_type: Mapped[str] = mapped_column(String(255), nullable=False)
    # "metadata" — зарезервированное имя атрибута в declarative → атрибут checkpoint_metadata, колонка metadata
    checkpoint_metadata: Mapped[bytes] = mapped_column("metadata", LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        # последний checkpoint треда + GC по last-activity
        Index("ix_react_checkpoint_thread_created", "thread_id", "checkpoint_ns", "created_at"),
    )


class ReactCheckpointWrite(Base):
    __tablename__ = "react_checkpoint_write"

    thread_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    checkpoint_ns: Mapped[str] = mapped_column(String(64), primary_key=True, default="")
    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idx: Mapped[int] = mapped_column(Integer, primary_key=True)  # ЗНАКОВЫЙ (спец-writes < 0)
    channel: Mapped[str] = mapped_column(String(128), nullable=False)
    write_type: Mapped[str] = mapped_column(String(255), nullable=False)
    blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    task_path: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_react_cp_write_lookup", "thread_id", "checkpoint_ns", "checkpoint_id"),
    )
