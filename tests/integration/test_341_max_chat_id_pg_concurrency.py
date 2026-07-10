"""#341 (F1) — Postgres-only concurrency test для атомарного guard'а
``max_chat_id`` (Codex R-codex MAJOR C).

SQLite не моделирует row-level locking с production-семантикой, поэтому
race «две сессии обе читают NULL и обе коммитят» проверяется на реальном
Postgres. Требует явного opt-in (та же схема, что
``tests/integration/test_message_queue_postgres_concurrency.py``):

  $ SREDA_TEST_POSTGRES_URL=postgresql://user:pw@localhost/sreda_test \
    SREDA_TEST_POSTGRES_DESTRUCTIVE_OPT_IN=1 \
    .venv/Scripts/python.exe -m pytest \
      tests/integration/test_341_max_chat_id_pg_concurrency.py -v

Без обеих env-переменных модуль пропускается (unit-сюита остаётся зелёной
без Postgres).
"""

from __future__ import annotations

import os
import threading

import pytest
from sqlalchemy import create_engine, or_ as sa_or, update as sa_update
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User

_POSTGRES_URL = os.environ.get("SREDA_TEST_POSTGRES_URL")
_DESTRUCTIVE_OPT_IN = (
    os.environ.get("SREDA_TEST_POSTGRES_DESTRUCTIVE_OPT_IN") == "1"
)

pytestmark = pytest.mark.skipif(
    not (_POSTGRES_URL and _DESTRUCTIVE_OPT_IN),
    reason="needs SREDA_TEST_POSTGRES_URL + SREDA_TEST_POSTGRES_DESTRUCTIVE_OPT_IN=1",
)


def _atomic_first_set(session, user_id: str, value: str) -> None:
    """Тот же conditional UPDATE, что в onboarding.ensure_max_user_bundle."""
    session.execute(
        sa_update(User)
        .where(User.id == user_id)
        .where(sa_or(User.max_chat_id.is_(None), User.max_chat_id == ""))
        .values(max_chat_id=value)
        .execution_options(synchronize_session=False)
    )
    session.commit()


def test_max_chat_id_atomic_guard_two_sessions_no_overwrite() -> None:
    engine = create_engine(_POSTGRES_URL)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    # Seed: юзер с NULL max_chat_id.
    s = SessionLocal()
    try:
        s.query(User).filter(User.id == "u_race_341").delete()
        s.query(Tenant).filter(Tenant.id == "t_race_341").delete()
        s.add(Tenant(id="t_race_341", name="Race"))
        s.add(User(
            id="u_race_341", tenant_id="t_race_341",
            max_account_id="race341", max_chat_id=None,
        ))
        s.commit()
    finally:
        s.close()

    barrier = threading.Barrier(2)

    def _worker(value: str) -> None:
        sess = SessionLocal()
        try:
            barrier.wait(timeout=10)
            _atomic_first_set(sess, "u_race_341", value)
        finally:
            sess.close()

    t1 = threading.Thread(target=_worker, args=("chatA",))
    t2 = threading.Thread(target=_worker, args=("chatB",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    s = SessionLocal()
    try:
        final = s.get(User, "u_race_341").max_chat_id
    finally:
        s.close()

    # Ровно ОДИН writer выиграл; второй (WHERE IS NULL OR '') не заматчил → не
    # перетёр. Значение стабильно одно из двух, не смешано/не последнее-побеждает.
    assert final in ("chatA", "chatB")

    # Established immutability: третий писатель с другим значением НЕ перезаписывает.
    s = SessionLocal()
    try:
        _atomic_first_set(s, "u_race_341", "chatC")
    finally:
        s.close()
    s = SessionLocal()
    try:
        assert s.get(User, "u_race_341").max_chat_id == final
    finally:
        s.close()
