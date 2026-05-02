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
# recall_with_stats — Stage 2 observability of memory retrieval
# (см. tomorrow-plan пункт 11). Tests on the diagnostic stats accumulator,
# not on hit ordering (recall() tests above already cover ordering).
# ---------------------------------------------------------------------------


def test_recall_with_stats_returns_hits_and_full_distribution(session, embeddings):
    """Happy path — three memories, no min_score filter (we use -1.0
    because fake hash-embeddings can give negative cosines for unrelated
    pairs and we don't want to test cosine semantics here, only the
    stats accumulator). All three should be seeded; distribution is
    populated."""
    repo = MemoryRepository(session)
    for i in range(3):
        repo.save(
            "t1", "u1",
            tier="core",
            content=f"fact {i}",
            embedding=embeddings.embed_document(f"fact {i}"),
        )
    session.commit()

    hits, stats = repo.recall_with_stats(
        "t1", "u1",
        embeddings.embed_query("fact 1"),
        top_k=10,
        min_score=-1.0,  # accept any cosine, including negatives
    )
    assert len(hits) == 3
    assert stats.candidates_total == 3
    assert stats.with_embedding == 3
    assert stats.filtered_below_min == 0
    assert stats.seeded_count == 3
    assert stats.top_k == 10
    assert stats.min_score == -1.0
    assert stats.scores_min is not None and stats.scores_max is not None
    assert stats.scores_min <= stats.scores_max
    assert stats.scores_p50 is not None


def test_recall_with_stats_counts_filtered_below_min(session, embeddings):
    """When min_score drops everything, stats should show
    filtered_below_min=N and seeded_count=0; scores_min/max/p50 must be
    None (no scores survived to compute percentiles over)."""
    repo = MemoryRepository(session)
    for i in range(5):
        repo.save(
            "t1", "u1",
            tier="core",
            content=f"a{i}",
            embedding=embeddings.embed_document(f"a{i}"),
        )
    session.commit()

    hits, stats = repo.recall_with_stats(
        "t1", "u1",
        embeddings.embed_query("totally unrelated zzz"),
        top_k=10,
        min_score=0.99,  # nothing survives
    )
    assert hits == []
    assert stats.candidates_total == 5
    assert stats.with_embedding == 5
    assert stats.filtered_below_min == 5
    assert stats.seeded_count == 0
    assert stats.scores_min is None
    assert stats.scores_max is None
    assert stats.scores_p50 is None


def test_recall_with_stats_counts_missing_embeddings(session, embeddings):
    """Memories saved without an embedding must NOT count as
    with_embedding; they're invisible to the cosine pipeline. Useful
    signal for when we need a re-embed migration."""
    repo = MemoryRepository(session)
    repo.save(
        "t1", "u1",
        tier="core",
        content="has embedding",
        embedding=embeddings.embed_document("has embedding"),
    )
    repo.save(
        "t1", "u1",
        tier="core",
        content="no embedding",
        embedding=None,
    )
    session.commit()

    hits, stats = repo.recall_with_stats(
        "t1", "u1",
        embeddings.embed_query("has embedding"),
        top_k=10,
        min_score=0.0,
    )
    assert stats.candidates_total == 2
    assert stats.with_embedding == 1   # only one had a vector
    assert stats.seeded_count == 1


def test_recall_with_stats_top_k_caps_seeded_but_distribution_is_full(session, embeddings):
    """Score distribution (min/max/p50) is computed across ALL passing
    scores, not just the top_k slice — otherwise we lose the long-tail
    visibility we want for tuning."""
    repo = MemoryRepository(session)
    for i in range(20):
        repo.save(
            "t1", "u1",
            tier="core",
            content=f"item {i}",
            embedding=embeddings.embed_document(f"item {i}"),
        )
    session.commit()

    hits, stats = repo.recall_with_stats(
        "t1", "u1",
        embeddings.embed_query("item 5"),
        top_k=5,
        min_score=-1.0,  # accept any cosine — see note above
    )
    assert len(hits) == 5
    assert stats.seeded_count == 5
    assert stats.candidates_total == 20
    # All 20 had embeddings and passed min_score=-1
    assert stats.with_embedding == 20
    assert stats.filtered_below_min == 0
    # Distribution is over all 20 passers (not just the 5 seeded)
    assert stats.scores_min is not None and stats.scores_max is not None


def test_recall_returns_same_hits_as_recall_with_stats(session, embeddings):
    """recall() is now a thin wrapper over recall_with_stats(). Make
    sure both paths produce identical hit lists for the same args, so
    no caller is silently affected by the refactor."""
    repo = MemoryRepository(session)
    for i in range(7):
        repo.save(
            "t1", "u1",
            tier="core",
            content=f"row {i}",
            embedding=embeddings.embed_document(f"row {i}"),
        )
    session.commit()

    query = embeddings.embed_query("row 3")
    hits_legacy = repo.recall("t1", "u1", query, top_k=4, min_score=0.0)
    hits_new, _ = repo.recall_with_stats(
        "t1", "u1", query, top_k=4, min_score=0.0,
    )
    assert [h.memory.id for h in hits_legacy] == [h.memory.id for h in hits_new]
    assert [h.score for h in hits_legacy] == [h.score for h in hits_new]


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
