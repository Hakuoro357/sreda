"""Phase 3b: memory CRUD + cosine recall."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.repositories.memory import MemoryRepository
from sreda.services.embeddings import FakeEmbeddingClient


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="T"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    sess.add(User(id="u2", tenant_id="t1", telegram_account_id="99"))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture()
def embeddings():
    return FakeEmbeddingClient()


# ---------------------------------------------------------------------------
# Save / list
# ---------------------------------------------------------------------------


def test_save_and_list(session, embeddings):
    repo = MemoryRepository(session)
    repo.save(
        "t1",
        "u1",
        tier="core",
        content="у меня дочь Маша 9 лет",
        embedding=embeddings.embed_document("у меня дочь Маша 9 лет"),
        source="user_direct",
    )
    repo.save(
        "t1",
        "u1",
        tier="episodic",
        content="вчера жаловался на сроки",
        embedding=embeddings.embed_document("вчера жаловался на сроки"),
    )
    session.commit()

    all_rows = repo.list_by_user("t1", "u1")
    assert len(all_rows) == 2

    core_only = repo.list_by_user("t1", "u1", tier="core")
    assert len(core_only) == 1
    assert core_only[0].content == "у меня дочь Маша 9 лет"
    assert core_only[0].source == "user_direct"


def test_save_rejects_invalid_tier(session):
    repo = MemoryRepository(session)
    with pytest.raises(ValueError, match="unknown tier"):
        repo.save("t1", "u1", tier="procedural", content="foo")


def test_save_rejects_invalid_source(session):
    repo = MemoryRepository(session)
    with pytest.raises(ValueError, match="unknown source"):
        repo.save("t1", "u1", tier="core", content="foo", source="phishing")


def test_save_rejects_empty_content(session):
    repo = MemoryRepository(session)
    with pytest.raises(ValueError, match="empty"):
        repo.save("t1", "u1", tier="core", content="   ")


def test_list_is_scoped_by_user(session, embeddings):
    repo = MemoryRepository(session)
    repo.save(
        "t1",
        "u1",
        tier="core",
        content="u1 fact",
        embedding=embeddings.embed_document("u1 fact"),
    )
    repo.save(
        "t1",
        "u2",
        tier="core",
        content="u2 fact",
        embedding=embeddings.embed_document("u2 fact"),
    )
    session.commit()

    u1_rows = repo.list_by_user("t1", "u1")
    assert len(u1_rows) == 1
    assert u1_rows[0].content == "u1 fact"


# ---------------------------------------------------------------------------
# Recall (cosine similarity with FakeEmbeddingClient)
# ---------------------------------------------------------------------------


def test_recall_ranks_exact_match_highest(session, embeddings):
    """Fake embeddings are deterministic hashes, so identical text has
    cosine 1.0 and different text has near-zero similarity. Good enough
    to prove the ranking logic."""
    repo = MemoryRepository(session)
    repo.save(
        "t1",
        "u1",
        tier="core",
        content="дочь Маша 9 лет",
        embedding=embeddings.embed_document("дочь Маша 9 лет"),
    )
    repo.save(
        "t1",
        "u1",
        tier="core",
        content="работаю в Химках",
        embedding=embeddings.embed_document("работаю в Химках"),
    )
    repo.save(
        "t1",
        "u1",
        tier="core",
        content="люблю пиццу",
        embedding=embeddings.embed_document("люблю пиццу"),
    )
    session.commit()

    # Query exactly matching one fact
    query_vec = embeddings.embed_query("дочь Маша 9 лет")
    hits = repo.recall("t1", "u1", query_vec, top_k=3)

    assert len(hits) <= 3
    # Top hit must be the exact-match row
    assert hits[0].memory.content == "дочь Маша 9 лет"
    # Its score should be 1.0 for identical embeddings
    assert abs(hits[0].score - 1.0) < 1e-6


def test_recall_filters_by_tier(session, embeddings):
    repo = MemoryRepository(session)
    repo.save(
        "t1",
        "u1",
        tier="core",
        content="core fact",
        embedding=embeddings.embed_document("core fact"),
    )
    repo.save(
        "t1",
        "u1",
        tier="episodic",
        content="episode fact",
        embedding=embeddings.embed_document("core fact"),  # intentionally same vector
    )
    session.commit()

    query = embeddings.embed_query("core fact")
    core_hits = repo.recall("t1", "u1", query, tier="core")
    episodic_hits = repo.recall("t1", "u1", query, tier="episodic")
    assert len(core_hits) == 1 and core_hits[0].memory.tier == "core"
    assert len(episodic_hits) == 1 and episodic_hits[0].memory.tier == "episodic"


def test_recall_respects_top_k(session, embeddings):
    repo = MemoryRepository(session)
    for i in range(10):
        repo.save(
            "t1",
            "u1",
            tier="core",
            content=f"fact {i}",
            embedding=embeddings.embed_document(f"fact {i}"),
        )
    session.commit()

    hits = repo.recall("t1", "u1", embeddings.embed_query("fact 5"), top_k=3)
    assert len(hits) == 3


def test_recall_skips_memories_without_embedding(session, embeddings):
    repo = MemoryRepository(session)
    # One with embedding, one without
    repo.save(
        "t1",
        "u1",
        tier="core",
        content="has embedding",
        embedding=embeddings.embed_document("has embedding"),
    )
    repo.save("t1", "u1", tier="core", content="no embedding")
    session.commit()

    hits = repo.recall("t1", "u1", embeddings.embed_query("has embedding"), top_k=5)
    assert len(hits) == 1
    assert hits[0].memory.content == "has embedding"


def test_recall_min_score_filters_noise(session, embeddings):
    repo = MemoryRepository(session)
    repo.save(
        "t1",
        "u1",
        tier="core",
        content="something",
        embedding=embeddings.embed_document("something"),
    )
    session.commit()

    # Use a completely unrelated query — fake embeddings are hash-based
    # so cosine similarity will be low. Setting a high threshold filters
    # it out.
    hits = repo.recall(
        "t1",
        "u1",
        embeddings.embed_query("zzz unrelated"),
        top_k=5,
        min_score=0.99,
    )
    assert hits == []


# ---------------------------------------------------------------------------
# touch_accessed / delete
# ---------------------------------------------------------------------------


def test_touch_accessed_increments_and_timestamps(session, embeddings):
    repo = MemoryRepository(session)
    row = repo.save(
        "t1",
        "u1",
        tier="core",
        content="fact",
        embedding=embeddings.embed_document("fact"),
    )
    session.commit()
    assert row.access_count == 0
    assert row.last_accessed_at is None

    repo.touch_accessed(row.id)
    repo.touch_accessed(row.id)
    session.commit()

    refreshed = repo.get(row.id)
    assert refreshed.access_count == 2
    assert refreshed.last_accessed_at is not None


def test_delete_returns_true_when_row_exists(session, embeddings):
    repo = MemoryRepository(session)
    row = repo.save(
        "t1",
        "u1",
        tier="core",
        content="fact",
        embedding=embeddings.embed_document("fact"),
    )
    session.commit()

    assert repo.delete(row.id) is True
    assert repo.get(row.id) is None
    # Second delete is a no-op
    assert repo.delete(row.id) is False
