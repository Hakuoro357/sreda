"""#232 способ Б — B1: репозиторий react_summaries (round-trip шифра + одна строка/тред + GC)."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.react_summary import ReactSummary
from sreda.services import react_summary_store as store


@pytest.fixture()
def session():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    yield s
    s.close()
    engine.dispose()
    try:
        os.unlink(path)
    except OSError:
        pass


def test_upsert_then_load_roundtrip(session):
    store.upsert_summary(
        session, thread_id="react-v1:abc", tenant_id="tenant_tg_1",
        text="ВЫЖИМКА: проект Орбита, дедлайн 14 марта", covered_message_count=8,
        covered_hash="deadbeefcafe", version=1, covered_through_ts=datetime.now(timezone.utc))
    session.commit()
    rec = store.load_summary(session, "react-v1:abc")
    assert rec == {
        "version": 1,
        "text": "ВЫЖИМКА: проект Орбита, дедлайн 14 марта",  # расшифровалось (EncryptedString)
        "covered_message_count": 8,
        "covered_hash": "deadbeefcafe",
    }


def test_load_missing_returns_none(session):
    assert store.load_summary(session, "нет-такого") is None


def test_upsert_replaces_one_row_per_thread(session):
    store.upsert_summary(session, thread_id="t", tenant_id="x", text="старая",
                         covered_message_count=4, covered_hash="h1", version=1)
    session.commit()
    store.upsert_summary(session, thread_id="t", tenant_id="x", text="новая",
                         covered_message_count=10, covered_hash="h2", version=1)
    session.commit()
    rec = store.load_summary(session, "t")
    assert rec["text"] == "новая" and rec["covered_message_count"] == 10 and rec["covered_hash"] == "h2"
    assert session.query(ReactSummary).count() == 1  # одна актуальная выжимка на тред


def test_gc_deletes_stale_only(session):
    store.upsert_summary(session, thread_id="old", tenant_id="x", text="t",
                         covered_message_count=4, covered_hash="h", version=1)
    store.upsert_summary(session, thread_id="fresh", tenant_id="x", text="t",
                         covered_message_count=4, covered_hash="h", version=1)
    session.commit()
    session.query(ReactSummary).filter_by(thread_id="old").update(
        {"updated_at": datetime.now(timezone.utc) - timedelta(days=40)})
    session.commit()
    n = store.delete_summaries_older_than(session, datetime.now(timezone.utc) - timedelta(days=30))
    session.commit()
    assert n == 1
    assert store.load_summary(session, "old") is None
    assert store.load_summary(session, "fresh") is not None


def test_upsert_monotonic_no_regression(session):
    """R2 MAJOR: upsert НЕ понижает covered_message_count (две фоновые задачи — побеждает БÓЛЬШЕЕ покрытие)."""
    store.upsert_summary(session, thread_id="t", tenant_id="x", text="cov20",
                         covered_message_count=20, covered_hash="h20", version=1)
    session.commit()
    # «старая» задача с меньшим покрытием закоммитилась позже — НЕ должна перезаписать большее
    store.upsert_summary(session, thread_id="t", tenant_id="x", text="cov15",
                         covered_message_count=15, covered_hash="h15", version=1)
    session.commit()
    rec = store.load_summary(session, "t")
    assert rec["covered_message_count"] == 20 and rec["text"] == "cov20"  # не регрессировали
    # бóльшее покрытие — обновляет
    store.upsert_summary(session, thread_id="t", tenant_id="x", text="cov30",
                         covered_message_count=30, covered_hash="h30", version=1)
    session.commit()
    rec2 = store.load_summary(session, "t")
    assert rec2["covered_message_count"] == 30 and rec2["text"] == "cov30"
