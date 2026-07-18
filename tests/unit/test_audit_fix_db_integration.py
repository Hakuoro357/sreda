"""Audit 2026-07-18 fix regression tests — slug ``db-integration``.

Стыковые доработки после парада фикс-воркеров (требования db-uniques):

1. ``message_queue.enqueue_message`` — принимает и пишет ``bot_key`` ЯВНО
   (default ``"sreda"`` только safety-net), pre-check фильтрует по
   ``bot_key``; ``derive_thread_key`` учитывает ``bot_key`` (FIFO двух
   ботов одного чата не склеивается).
2. ``free_tier.FreeTierCounter._get_or_create`` — IntegrityError →
   rollback → re-resolve (FC-4, unique ``ix_free_tier_usage_unique``
   уже есть в БД).
3. ``retention_cleanup`` — ``message_jobs`` в ретенции (cross-security
   N1, вторая половина): терминальные done/failed/dead_letter старше
   30 дней удаляются, живые не трогаются.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from sreda.db.models import MessageJob
from sreda.db.models.free_tier import FreeTierUsage
from sreda.maintenance.retention_cleanup import cleanup_runtime_retention
from sreda.services.free_tier import FreeTierCounter
from sreda.workers.message_queue import (
    DuplicateMessageJob,
    derive_thread_key,
    enqueue_message,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 1a. enqueue_message: bot_key пишется явно, дедуп-ключ включает bot_key
# ---------------------------------------------------------------------------


def _enqueue(db_session: Session, **overrides: object) -> MessageJob:
    kwargs = dict(
        tenant_id="tenant_1",
        thread_id="thread_1",
        channel="telegram",
        external_update_id="42",
        message_payload={"text": "привет"},
    )
    kwargs.update(overrides)
    return enqueue_message(db_session, **kwargs)  # type: ignore[arg-type]


def test_enqueue_writes_explicit_bot_key(db_session: Session) -> None:
    job = _enqueue(db_session, bot_key="sreda_home")
    assert job.bot_key == "sreda_home"


def test_enqueue_default_bot_key_is_safety_net(db_session: Session) -> None:
    """Insert без bot_key не ломается — default 'sreda' (контракт
    OutboxMessage.bot_key), но это ТОЛЬКО safety-net."""
    job = _enqueue(db_session)
    assert job.bot_key == "sreda"


def test_enqueue_same_update_different_bots_both_land(db_session: Session) -> None:
    """Ключевая регрессия db-migrations #1 на уровне producer'а: update 42
    бота sreda и update 42 бота sreda_home — РАЗНЫЕ события, pre-check не
    должен схлопывать их в один namespace."""
    _enqueue(db_session, bot_key="sreda")
    other = _enqueue(db_session, bot_key="sreda_home", thread_id="thread_2")
    assert other.id is not None
    assert db_session.query(MessageJob).count() == 2


def test_enqueue_same_update_same_bot_raises_duplicate(db_session: Session) -> None:
    """Ределивери того же update тем же ботом — дедуп как раньше."""
    first = _enqueue(db_session, bot_key="sreda")
    with pytest.raises(DuplicateMessageJob) as exc_info:
        _enqueue(db_session, bot_key="sreda", thread_id="thread_2")
    assert exc_info.value.existing.id == first.id


# ---------------------------------------------------------------------------
# 1b. derive_thread_key: bot_key в FIFO-ключе
# ---------------------------------------------------------------------------


def test_derive_thread_key_different_bots_get_different_keys() -> None:
    """Два бота одного tenant'а в одном чате — РАЗНЫЕ FIFO-потоки."""
    a = derive_thread_key("tenant_1", "telegram", "chat_42", bot_key="sreda")
    b = derive_thread_key("tenant_1", "telegram", "chat_42", bot_key="sreda_home")
    assert a != b


def test_derive_thread_key_default_bot_key_backward_compatible() -> None:
    """Legacy 3-arg вызов = bot_key 'sreda' (safety-net default)."""
    assert derive_thread_key("tenant_1", "telegram", "chat_42") == derive_thread_key(
        "tenant_1", "telegram", "chat_42", bot_key="sreda"
    )


def test_derive_thread_key_same_bot_still_deterministic() -> None:
    a = derive_thread_key("tenant_1", "telegram", "chat_42", bot_key="sreda_home")
    b = derive_thread_key("tenant_1", "telegram", "chat_42", bot_key="sreda_home")
    assert a == b


# ---------------------------------------------------------------------------
# 2. free_tier._get_or_create: IntegrityError → rollback → re-resolve (FC-4)
# ---------------------------------------------------------------------------


def _committing_factory():
    """Свежая КОММИТЯЩАЯ in-memory БД на тест (rollback внутри
    _get_or_create откатывает только свою TX — засев победителя гонки
    должен пережить его, поэтому db_session с внешней TX не подходит)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    FreeTierUsage.__table__.create(engine)
    return sessionmaker(bind=engine)


def test_free_tier_get_or_create_reresolves_after_lost_race(monkeypatch) -> None:
    """Гонка двух первых запросов дня: наш SELECT строку не увидел,
    INSERT уперся в unique → rollback → re-resolve возвращает строку
    победителя (с её счётчиком), без 500."""
    factory = _committing_factory()
    today = date(2026, 7, 18)
    with factory() as seed:
        seed.add(
            FreeTierUsage(
                id="ftu_racer", tenant_id="t1", user_id="u1",
                day=today, llm_calls=3, updated_at=_now(),
            )
        )
        seed.commit()

    with factory() as session:
        counter = FreeTierCounter(session)
        real_find = counter._find
        calls = {"n": 0}

        def stale_find(*args: object) -> FreeTierUsage | None:
            # Первый SELECT «не видит» concurrent insert — это и есть гонка.
            calls["n"] += 1
            if calls["n"] == 1:
                return None
            return real_find(*args)

        monkeypatch.setattr(counter, "_find", stale_find)
        row = counter._get_or_create("t1", "u1", today)
        assert row.id == "ftu_racer"
        assert row.llm_calls == 3  # счётчик победителя не затёрт


def test_free_tier_get_or_create_no_racer_reraises(monkeypatch) -> None:
    """IntegrityError НЕ от нашего unique (строки-победителя нет) —
    не маскируем, пробрасываем."""
    from sqlalchemy.exc import IntegrityError

    factory = _committing_factory()
    today = date(2026, 7, 18)
    with factory() as session:
        counter = FreeTierCounter(session)
        monkeypatch.setattr(counter, "_find", lambda *args: None)

        def boom_flush(*args: object, **kwargs: object) -> None:
            raise IntegrityError("INSERT INTO free_tier_usage", {}, Exception("ck"))

        monkeypatch.setattr(session, "flush", boom_flush)
        with pytest.raises(IntegrityError):
            counter._get_or_create("t9", "u9", today)


def test_free_tier_increment_and_check_happy_path_intact(db_session: Session) -> None:
    """Базовый путь не сломан рефакторингом: +1, возврат (count, over)."""
    counter = FreeTierCounter(db_session)
    count, over = counter.increment_and_check(tenant_id="t1", user_id="u1")
    assert count == 1
    assert over is False
    assert counter.usage_today(tenant_id="t1", user_id="u1") == 1


# ---------------------------------------------------------------------------
# 3. retention_cleanup: message_jobs в ретенции (cross-security N1)
# ---------------------------------------------------------------------------


def _mj(job_id: str, status: str, enqueued_at: datetime) -> MessageJob:
    """MessageJob с timestamps, удовлетворяющими CHECK-констрейнтам статуса."""
    base = dict(
        id=job_id,
        tenant_id="tenant_1",
        thread_id="thread_1",
        channel="telegram",
        external_update_id=f"upd_{job_id}",
        bot_key="sreda",
        message_payload={"text": "x"},
        status=status,
        enqueued_at=enqueued_at,
        attempt=1,
    )
    if status == "processing":
        base["started_at"] = enqueued_at
        base["lease_expires_at"] = enqueued_at + timedelta(seconds=300)
    elif status in ("done", "failed", "dead_letter"):
        base["started_at"] = enqueued_at
        base["finished_at"] = enqueued_at + timedelta(seconds=5)
    return MessageJob(**base)  # type: ignore[arg-type]


def test_retention_deletes_terminal_message_jobs(db_session: Session) -> None:
    now = _now()
    old = now - timedelta(days=40)
    fresh = now - timedelta(days=1)
    db_session.add_all([
        _mj("job_done_old", "done", old),
        _mj("job_failed_old", "failed", old),
        _mj("job_dead_old", "dead_letter", old),
        _mj("job_done_fresh", "done", fresh),
        _mj("job_pending_old", "pending", old),
        _mj("job_processing_old", "processing", old),
    ])
    db_session.flush()

    result = cleanup_runtime_retention(db_session, now=now)

    assert result.message_jobs == 3
    assert result.total >= result.message_jobs
    survivors = {j.id for j in db_session.query(MessageJob).all()}
    assert survivors == {"job_done_fresh", "job_pending_old", "job_processing_old"}


def test_retention_message_jobs_empty_table_noop(db_session: Session) -> None:
    result = cleanup_runtime_retention(db_session, now=_now())
    assert result.message_jobs == 0
