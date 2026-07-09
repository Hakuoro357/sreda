"""#193: кастомный durable checkpoint-saver для ReAct-цикла (вариант B).

``EncryptedSqlCheckpointSaver`` — ``BaseCheckpointSaver`` на нашем SYNC SQLAlchemy-движке
(``get_session_factory``). Граф ReAct async → дефолтные ``aget_tuple/aput/aput_writes/alist`` базы
БРОСАЮТ ``NotImplementedError`` → реализованы как ``asyncio.to_thread(sync-ядро)``. Своя сессия per-op
(как ``react_trace_persist._session``), НЕ делим с сессией запроса/графа (thread-safety под to_thread).

Шифрование — штатным ``EncryptedSerializer(SredaCipher, JsonPlusSerializer)``: каждый объект
(checkpoint / metadata / write) шифруется на уровне ``serde.dumps_typed`` → ``(type, ciphertext)`` →
BYTEA + type-колонка. Нет риска «забыли зашифровать канал X».

Layout — 1-blob inline: ВЕСЬ checkpoint (incl channel_values) в один ``blob`` (НЕ выносим
channel_values). writes — 2-я таблица; ``idx`` ЗНАКОВЫЙ; ``put_writes`` дедуп: idx≥0 → DO NOTHING
(повтор replay), idx<0 (спец) → DO UPDATE.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import datetime, timezone
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from sqlalchemy import delete, select

from sreda.db.models.react_checkpoint import ReactCheckpoint, ReactCheckpointWrite
from sreda.db.session import get_session_factory, tenant_ctx
from sreda.services.encryption import get_encryption_service

logger = logging.getLogger(__name__)


def _dialect_insert(bind):
    """ON CONFLICT upsert по диалекту (PG в проде, sqlite в тестах) — атомарно, без гонки
    get-then-add при конкурентном replay (CR R1 MAJOR, паритет со штатным PostgresSaver)."""
    name = bind.dialect.name
    if name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
        return insert
    if name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
        return insert
    raise NotImplementedError(f"EncryptedSqlCheckpointSaver: диалект {name} не поддержан")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SredaCipher:
    """CipherProtocol поверх нашего ``EncryptionService`` (AES-256-GCM, ротация по key_id).

    ``encrypt(plaintext: bytes) -> (ciphername, ciphertext)`` где ciphername = key_id (decrypt выберет
    нужный ключ при ротации). EncryptedSerializer вшивает ciphername в type-строку, так что он
    переживает round-trip через ``checkpoint_type``/``write_type``.
    """

    def encrypt(self, plaintext: bytes) -> tuple[str, bytes]:
        return get_encryption_service().encrypt_bytes(plaintext)

    def decrypt(self, ciphername: str, ciphertext: bytes) -> bytes:
        return get_encryption_service().decrypt_bytes(ciphername, ciphertext)


def make_encrypted_serde() -> SerializerProtocol:
    """EncryptedSerializer(SredaCipher, JsonPlusSerializer) — шифрует каждый serde-объект."""
    return EncryptedSerializer(SredaCipher(), JsonPlusSerializer())


class EncryptedSqlCheckpointSaver(BaseCheckpointSaver):
    """Durable checkpoint-saver на sync-движке Среды с полным шифрованием payload."""

    def __init__(self, session_factory=None, serde: SerializerProtocol | None = None) -> None:
        super().__init__(serde=serde or make_encrypted_serde())
        self._sf = session_factory or get_session_factory()

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _cfg(thread_id: str, ns: str, checkpoint_id: str) -> RunnableConfig:
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": ns,
                "checkpoint_id": checkpoint_id,
            }
        }

    @staticmethod
    def _tenant_for_write(session, thread_id: str, op: str) -> str | None:
        """#138 Ф3-b: тенант для штампа новых строк + cross-check анти-подмены.

        Возвращает tenant_ctx (может быть None — легаси/фон, тогда warning РАЗ на вызов:
        строка уйдёт с tenant_id NULL = fail-closed видимость под RLS Ф3-c).
        Cross-check: если у треда УЖЕ есть checkpoint с tenant_id NOT NULL и он отличается
        от ctx-тенанта — RuntimeError, чужой тред НИКОГДА не дописываем. Один лёгкий SELECT
        tenant_id на вызов (индекс по thread_id в составе PK)."""
        ctx_tenant = tenant_ctx.get()
        if ctx_tenant is None:
            logger.warning(
                "react_checkpoint: запись без tenant_ctx thread=%s op=%s — tenant_id=NULL",
                thread_id, op,
            )
            return None
        existing = session.execute(
            select(ReactCheckpoint.tenant_id)
            .where(
                ReactCheckpoint.thread_id == thread_id,
                ReactCheckpoint.tenant_id.is_not(None),
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None and existing != ctx_tenant:
            raise RuntimeError(
                "react_checkpoint: tenant mismatch thread="
                f"{thread_id} existing={existing!r} ctx={ctx_tenant!r} — запись в чужой тред запрещена"
            )
        return ctx_tenant

    def _row_to_tuple(self, session, row: ReactCheckpoint) -> CheckpointTuple | None:
        """Собрать CheckpointTuple из строки checkpoint + её writes. None при ошибке расшифровки
        (граф стартует свежим — без падения и без утечки payload)."""
        try:
            checkpoint: Checkpoint = self.serde.loads_typed((row.checkpoint_type, row.blob))
            metadata: CheckpointMetadata = self.serde.loads_typed(
                (row.metadata_type, row.checkpoint_metadata)
            )
        except Exception:  # noqa: BLE001 — битый/нерасшифруемый blob → свежий старт, не падать
            logger.warning(
                "react_checkpoint decrypt/deserialize failed thread=%s ns=%s cp=%s — treat as fresh",
                row.thread_id, row.checkpoint_ns, row.checkpoint_id,
            )
            return None

        write_rows = session.execute(
            select(ReactCheckpointWrite)
            .where(
                ReactCheckpointWrite.thread_id == row.thread_id,
                ReactCheckpointWrite.checkpoint_ns == row.checkpoint_ns,
                ReactCheckpointWrite.checkpoint_id == row.checkpoint_id,
            )
            .order_by(ReactCheckpointWrite.task_id, ReactCheckpointWrite.idx)
        ).scalars().all()
        pending_writes = [
            (w.task_id, w.channel, self.serde.loads_typed((w.write_type, w.blob)))
            for w in write_rows
        ]
        parent_config = (
            self._cfg(row.thread_id, row.checkpoint_ns, row.parent_checkpoint_id)
            if row.parent_checkpoint_id
            else None
        )
        return CheckpointTuple(
            config=self._cfg(row.thread_id, row.checkpoint_ns, row.checkpoint_id),
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=parent_config,
            pending_writes=pending_writes,
        )

    # ---- sync core ------------------------------------------------------

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        cp_id = checkpoint["id"]
        parent = config["configurable"].get("checkpoint_id")
        c_type, c_blob = self.serde.dumps_typed(checkpoint)
        # get_checkpoint_metadata: чистит управляющие символы + мёрджит config-метаданные (контракт savers)
        m_type, m_blob = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        with self._sf() as s:
            # #138 Ф3-b: штамп тенанта + cross-check (чужой тред → RuntimeError)
            tenant_id = self._tenant_for_write(s, thread_id, "put")
            cols = {
                "thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": cp_id,
                "parent_checkpoint_id": parent, "checkpoint_type": c_type, "blob": c_blob,
                "metadata_type": m_type, "metadata": m_blob, "tenant_id": tenant_id,
                "created_at": _now(),
            }
            set_ = {
                "parent_checkpoint_id": parent, "checkpoint_type": c_type, "blob": c_blob,
                "metadata_type": m_type, "metadata": m_blob,
            }
            # tenant_id в UPDATE только при не-NULL ctx: не затираем бэкфилл/прежний штамп NULL'ом
            if tenant_id is not None:
                set_["tenant_id"] = tenant_id
            ins = _dialect_insert(s.bind)
            # атомарный upsert по PK-тройке (нет гонки get-then-add); created_at НЕ перезаписываем
            stmt = ins(ReactCheckpoint.__table__).values(**cols).on_conflict_do_update(
                index_elements=["thread_id", "checkpoint_ns", "checkpoint_id"],
                set_=set_,
            )
            s.execute(stmt)
            s.commit()
        return self._cfg(thread_id, ns, cp_id)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        cp_id = config["configurable"]["checkpoint_id"]
        pk = ["thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"]
        with self._sf() as s:
            # #138 Ф3-b: штамп тенанта + cross-check РАЗ на вызов (не пер-строку)
            tenant_id = self._tenant_for_write(s, thread_id, "put_writes")
            ins = _dialect_insert(s.bind)
            for i, (channel, value) in enumerate(writes):
                widx = WRITES_IDX_MAP.get(channel, i)
                w_type, w_blob = self.serde.dumps_typed(value)
                vals = {
                    "thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": cp_id,
                    "task_id": task_id, "idx": widx, "channel": channel,
                    "write_type": w_type, "blob": w_blob, "task_path": task_path,
                    "tenant_id": tenant_id, "created_at": _now(),
                }
                stmt = ins(ReactCheckpointWrite.__table__).values(**vals)
                if widx >= 0:
                    # обычный write: дедуп replay — повтор НЕ перезаписывает (атомарно, без гонки)
                    stmt = stmt.on_conflict_do_nothing(index_elements=pk)
                else:
                    # спец-write (interrupt/resume/error) → обновляем
                    set_ = {"channel": channel, "write_type": w_type,
                            "blob": w_blob, "task_path": task_path}
                    # не затираем не-NULL tenant_id NULL'ом (симметрично put)
                    if tenant_id is not None:
                        set_["tenant_id"] = tenant_id
                    stmt = stmt.on_conflict_do_update(index_elements=pk, set_=set_)
                s.execute(stmt)
            s.commit()

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        cp_id = get_checkpoint_id(config)
        with self._sf() as s:
            if cp_id:
                row = s.get(ReactCheckpoint, (thread_id, ns, cp_id))
            else:
                row = s.execute(
                    select(ReactCheckpoint)
                    .where(
                        ReactCheckpoint.thread_id == thread_id,
                        ReactCheckpoint.checkpoint_ns == ns,
                    )
                    .order_by(ReactCheckpoint.checkpoint_id.desc())
                    .limit(1)
                ).scalar_one_or_none()
            if row is None:
                return None
            return self._row_to_tuple(s, row)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        if filter:
            # metadata зашифрована → фильтр по ней не поддерживаем (ReAct грузит последний checkpoint)
            raise NotImplementedError(
                "EncryptedSqlCheckpointSaver.list(filter=) не поддерживается: metadata зашифрована"
            )
        q = select(ReactCheckpoint)
        if config is not None:
            thread_id = config["configurable"]["thread_id"]
            q = q.where(ReactCheckpoint.thread_id == thread_id)
            # ns фильтруем только если ключ задан явно (контракт savers)
            if "checkpoint_ns" in config["configurable"]:
                q = q.where(ReactCheckpoint.checkpoint_ns == config["configurable"]["checkpoint_ns"])
            # exact checkpoint_id, если указан (history/time-travel)
            cp_id = get_checkpoint_id(config)
            if cp_id:
                q = q.where(ReactCheckpoint.checkpoint_id == cp_id)
        if before is not None:
            bid = get_checkpoint_id(before)
            if bid:
                q = q.where(ReactCheckpoint.checkpoint_id < bid)
        q = q.order_by(ReactCheckpoint.checkpoint_id.desc())
        if limit is not None:  # limit=0 → пусто (не «все»)
            q = q.limit(limit)
        with self._sf() as s:
            rows = s.execute(q).scalars().all()
            for row in rows:
                t = self._row_to_tuple(s, row)
                if t is not None:
                    yield t

    # ---- наш метод: гашение протухшей паузы ----------------------------

    def clear_pending(self, thread_id: str, checkpoint_ns: str = "") -> int:
        """Снять протухшую durable-паузу: удалить interrupt/resume-writes (idx<0) ПОСЛЕДНЕГО
        checkpoint, сохранив сам checkpoint и обычные writes (idx≥0). Возвращает число удалённых
        строк. (R1-фикс gen: durable-ключ стабилен, паузу гасим явно, не сменой ключа.)"""
        with self._sf() as s:
            last_id = s.execute(
                select(ReactCheckpoint.checkpoint_id)
                .where(
                    ReactCheckpoint.thread_id == thread_id,
                    ReactCheckpoint.checkpoint_ns == checkpoint_ns,
                )
                .order_by(ReactCheckpoint.checkpoint_id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if last_id is None:
                return 0
            res = s.execute(
                delete(ReactCheckpointWrite).where(
                    ReactCheckpointWrite.thread_id == thread_id,
                    ReactCheckpointWrite.checkpoint_ns == checkpoint_ns,
                    ReactCheckpointWrite.checkpoint_id == last_id,
                    ReactCheckpointWrite.idx < 0,
                )
            )
            s.commit()
            return res.rowcount or 0

    def delete_thread(self, thread_id: str) -> None:
        """Удалить ВЕСЬ тред (обе таблицы, все ns) — для GC и recovery от poison-checkpoint
        (краш-луп: durable-ключ стабилен → сброс треда = аналог gen-bump для эфемерного пути)."""
        with self._sf() as s:
            s.execute(delete(ReactCheckpointWrite).where(
                ReactCheckpointWrite.thread_id == thread_id))
            s.execute(delete(ReactCheckpoint).where(
                ReactCheckpoint.thread_id == thread_id))
            s.commit()

    async def adelete_thread(self, thread_id: str) -> None:
        return await asyncio.to_thread(self.delete_thread, thread_id)

    # ---- async (to_thread обёртки; дефолтные методы базы RAISE) ---------

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        return await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        items = await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        )
        for item in items:
            yield item
