"""#232 способ Б: доступ к durable-выжимке истории (таблица react_summaries).

Хранилище отдельно от чекпойнта (#193) — выжимка не относится к таймлайну разговора (см.
db/models/react_summary.py). Функции БЕЗ commit — транзакцией управляет вызывающий (фасад/handle_turn).

- load_summary: → dict {version,text,covered_message_count,covered_hash} для build_model_input(summary=),
  либо None. Форма совпадает с make_summary_record (storage-agnostic потребление шага A не меняется).
- upsert_summary: одна актуальная выжимка на тред (PK = durable thread_id).
- delete_summaries_older_than: GC по updated_at (ветка retention, 30д).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from sreda.db.models.react_summary import ReactSummary


def load_summary(session: Session, thread_id: str) -> dict | None:
    """Актуальная выжимка треда в форме для build_model_input(summary=), либо None."""
    row = session.get(ReactSummary, thread_id)
    if row is None:
        return None
    return {
        "version": row.summary_version,
        "text": row.summary_text,
        "covered_message_count": row.covered_message_count,
        "covered_hash": row.covered_hash,
    }


def upsert_summary(
    session: Session,
    *,
    thread_id: str,
    tenant_id: str | None,
    text: str,
    covered_message_count: int,
    covered_hash: str,
    version: int,
    covered_through_ts: datetime | None = None,
) -> None:
    """Вставить-или-обновить выжимку треда (одна строка на тред; общий снимок разговора не трогаем).

    АТОМАРНЫЙ МОНОТОННЫЙ upsert (R3 MAJOR): `INSERT ... ON CONFLICT(thread_id) DO UPDATE ... WHERE
    covered_message_count < excluded.covered_message_count`. Гонко-безопасен НА УРОВНЕ БД: и обгон старой
    задачи, и одновременный первый-insert двух фоновых пересказов разрешаются БД (не read-then-write) —
    covered_message_count НИКОГДА не регрессирует, проигравший insert становится no-op UPDATE. Вызывающий
    коммитит."""
    new_n = int(covered_message_count)
    vals = {
        "tenant_id": tenant_id,
        "summary_text": text,
        "covered_message_count": new_n,
        "covered_hash": covered_hash,
        "summary_version": int(version),
        "covered_through_ts": covered_through_ts,
        "updated_at": datetime.now(timezone.utc),
    }
    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _insert
    else:  # незнакомый диалект → безопасный read-then-write (не прод; монотонность в своей сессии держит)
        row = session.get(ReactSummary, thread_id)
        if row is not None and row.covered_message_count >= new_n:
            return
        if row is None:
            row = ReactSummary(thread_id=thread_id)
            session.add(row)
        for _k, _v in vals.items():
            setattr(row, _k, _v)
        return
    stmt = _insert(ReactSummary).values(thread_id=thread_id, **vals)
    stmt = stmt.on_conflict_do_update(
        index_elements=["thread_id"],
        set_={k: getattr(stmt.excluded, k) for k in vals},
        where=ReactSummary.covered_message_count < stmt.excluded.covered_message_count,
    )
    session.execute(stmt)


def delete_summaries_older_than(session: Session, cutoff: datetime) -> int:
    """Удалить выжимки, не обновлявшиеся с cutoff (GC). Возвращает число удалённых. Вызывающий коммитит."""
    return (
        session.query(ReactSummary)
        .filter(ReactSummary.updated_at < cutoff)
        .delete(synchronize_session=False)
    )
