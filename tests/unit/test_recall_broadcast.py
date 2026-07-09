"""Phase 3 — recall_broadcast: fan-out across memory + checklist + reminder.

Lock-in tests for the design decisions taken during qwen-loop review
of `recall-broadcast-fanout` plan (final R3, 2026-05-04):

* R1#8 — error isolation per source
* R1#9 — pairwise cosine dedup at threshold > 0.95
* R2#1 — min_per_source=1 guarantee (без него global top_k=3 убил бы
  всю фичу — memory hits всегда выше checklist/reminder)
* Empty / NULL-embedding edge cases (R1#13)

Tests use ``FakeEmbeddingClient`` (deterministic 64-dim hash) and craft
embeddings directly to control cosine similarity precisely.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.checklists import Checklist, ChecklistItem
from sreda.db.models.core import Tenant, User
from sreda.db.models.housewife import FamilyReminder
from sreda.db.repositories.memory import (
    BroadcastHit,
    MemoryRepository,
    _dedup_pairwise,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="T"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="42"))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


def _vec(seed: int, dim: int = 16) -> list[float]:
    """Generate a deterministic, normalised vector for a given seed.

    Different seeds → near-orthogonal vectors → low cosine.
    Same seed → identical vector → cosine 1.0 (тест dedup).
    """
    import math
    raw = [(seed * (i + 1)) % 7 - 3 for i in range(dim)]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


# ---------------------------------------------------------------------------
# Empty / edge cases
# ---------------------------------------------------------------------------


def test_recall_broadcast_empty_store_returns_empty(session):
    repo = MemoryRepository(session)
    hits, stats = repo.recall_broadcast("t1", "u1", _vec(1))
    assert hits == []
    assert stats["memory"].count == 0
    assert stats["checklist"].count == 0
    assert stats["reminder"].count == 0


def test_recall_broadcast_skips_null_embedding_rows(session):
    """NULL embedding_json — row пропускается без crash'а."""
    repo = MemoryRepository(session)
    # save a memory WITH embedding
    repo.save("t1", "u1", tier="core", content="имеет",
              embedding=_vec(1), source="user_direct")
    # raw insert a memory WITHOUT embedding_json (legacy row).
    # #262: category_id теперь NOT NULL — кладём в Common (ensure_common идемпотентен, save выше его создал).
    from sreda.db.models.memory import AssistantMemory
    legacy = AssistantMemory(
        id="mem_legacy",
        tenant_id="t1",
        user_id="u1",
        tier="core",
        content="legacy без эмбеддинга",
        embedding_json=None,
        embedding_dim=0,
        source="user_direct",
        category_id=repo.ensure_common("t1", "u1"),
    )
    session.add(legacy)
    session.commit()

    hits, stats = repo.recall_broadcast("t1", "u1", _vec(1))
    # должен найти 1 memory (с эмбеддингом), legacy пропустить
    assert stats["memory"].count == 1
    contents = [h.content for h in hits]
    assert "имеет" in contents
    assert "legacy без эмбеддинга" not in contents


# ---------------------------------------------------------------------------
# Cross-source merging
# ---------------------------------------------------------------------------


def _add_checklist_with_item(
    session, *, list_id: str, item_id: str, list_title: str,
    item_title: str, embedding: list[float],
) -> None:
    cl = Checklist(
        id=list_id, tenant_id="t1", user_id="u1",
        title=list_title, status="active",
    )
    session.add(cl)
    item = ChecklistItem(
        id=item_id, checklist_id=list_id, tenant_id="t1",
        position=0, title=item_title, status="pending",
        embedding_json=json.dumps(embedding),
        embedding_model="e5-large",
    )
    session.add(item)
    session.commit()


def _add_reminder(
    session, *, rem_id: str, title: str, embedding: list[float],
) -> None:
    rem = FamilyReminder(
        id=rem_id, tenant_id="t1", user_id="u1",
        title=title,
        trigger_at=datetime(2026, 5, 18, 9, tzinfo=timezone.utc),
        next_trigger_at=datetime(2026, 5, 18, 9, tzinfo=timezone.utc),
        status="pending",
        embedding_json=json.dumps(embedding),
        embedding_model="e5-large",
    )
    session.add(rem)
    session.commit()


def test_recall_broadcast_merges_three_sources(session):
    """Memory + checklist + reminder — все три попадают в final.

    Seeds 1/2/4 дают pairwise cos 0.42-0.49 — все > min_score=0.1
    (попадают в результаты), все < 0.95 (не схлопываются dedup'ом).
    """
    repo = MemoryRepository(session)
    repo.save("t1", "u1", tier="core", content="из памяти",
              embedding=_vec(1), source="user_direct")
    _add_checklist_with_item(
        session, list_id="cl1", item_id="ci1",
        list_title="Список", item_title="из чеклиста",
        embedding=_vec(2),
    )
    _add_reminder(session, rem_id="rem1", title="из напоминания",
                  embedding=_vec(4))

    hits, stats = repo.recall_broadcast("t1", "u1", _vec(1), top_k=10)
    sources = {h.source.split(":")[0] for h in hits}
    assert "memory" in sources
    assert "checklist" in sources
    assert "reminder" in sources
    assert stats["memory"].count == 1
    assert stats["checklist"].count == 1
    assert stats["reminder"].count == 1


def test_include_structured_false_skips_checklists_and_reminders(session):
    """Backward-compat: include_structured=False → memory only."""
    repo = MemoryRepository(session)
    repo.save("t1", "u1", tier="core", content="memory",
              embedding=_vec(1), source="user_direct")
    _add_checklist_with_item(
        session, list_id="cl1", item_id="ci1",
        list_title="L", item_title="from checklist", embedding=_vec(1),
    )
    _add_reminder(session, rem_id="rem1", title="reminder", embedding=_vec(1))

    hits, stats = repo.recall_broadcast(
        "t1", "u1", _vec(1), include_structured=False,
    )
    assert all(h.source.startswith("memory:") for h in hits)
    assert "checklist" not in stats
    assert "reminder" not in stats


# ---------------------------------------------------------------------------
# min_per_source=1 (R2#1) — самое важное поведенческое требование
# ---------------------------------------------------------------------------


def test_min_per_source_promotes_low_score_checklist_hit(session):
    """top_k=2 с 5 высокими memory hits и 1 низким checklist hit:
    final должен содержать ≥1 checklist hit (через min_per_source=1)."""
    repo = MemoryRepository(session)
    # 5 memory hits c near-identical query (high score)
    for i in range(5):
        repo.save(
            "t1", "u1", tier="core",
            content=f"memory #{i}",
            embedding=_vec(1),  # все одинаковые — тот же score
            source="user_direct",
        )
    # one checklist hit с очень другим вектором (низкий cos но >0.1)
    # _vec(2) даст ~0.5+ cos с _vec(1) для нашего hash-paste; это >0.1
    _add_checklist_with_item(
        session, list_id="cl1", item_id="ci1",
        list_title="L", item_title="checklist item",
        embedding=_vec(2),
    )

    hits, _stats = repo.recall_broadcast("t1", "u1", _vec(1), top_k=2)
    sources = {h.source.split(":")[0] for h in hits}
    # без guarantee final был бы 2 memory; с guarantee есть checklist
    assert "checklist" in sources, (
        f"min_per_source=1 broken: final sources={sources}, "
        f"top_k=2 cap should not silence checklist if it has ≥1 hit"
    )


def test_min_per_source_below_min_score_not_promoted(session):
    """Если у source ВСЕ scores ниже min_score — promotion не делается
    (правило: «≥1 hit с score ≥ min_score»)."""
    repo = MemoryRepository(session)
    repo.save("t1", "u1", tier="core", content="m",
              embedding=_vec(1), source="user_direct")
    # checklist item с заведомо низким cos (negative seed, dim mismatch)
    bad_vec = [-1.0] * 16
    _add_checklist_with_item(
        session, list_id="cl1", item_id="ci1",
        list_title="L", item_title="низкий score",
        embedding=bad_vec,
    )

    hits, _stats = repo.recall_broadcast(
        "t1", "u1", _vec(1), top_k=10, min_score=0.5,
    )
    sources = {h.source for h in hits}
    # checklist hit не должен попасть — его score ниже min_score
    assert not any(s.startswith("checklist") for s in sources)


# ---------------------------------------------------------------------------
# Pairwise dedup (R1#9)
# ---------------------------------------------------------------------------


def test_dedup_pairwise_drops_near_duplicate_embeddings():
    """Same embedding в двух разных hits → dedup keeps higher score."""
    h_high = BroadcastHit(
        source="memory:core", content="same fact", score=0.9,
        id="m1", embedding=_vec(1), metadata={},
    )
    h_low = BroadcastHit(
        source="checklist:cl1", content="same fact",  score=0.7,
        id="ci1", embedding=_vec(1), metadata={},
    )
    h_other = BroadcastHit(
        source="reminder:rem1", content="other", score=0.5,
        id="r1", embedding=_vec(2), metadata={},
    )

    result = _dedup_pairwise([h_high, h_low, h_other])
    ids = {h.id for h in result}
    # h_low выкинут (cos=1.0 с h_high, но score ниже)
    assert "m1" in ids
    assert "ci1" not in ids
    assert "r1" in ids


def test_dedup_pairwise_empty_list_safe():
    assert _dedup_pairwise([]) == []


# ---------------------------------------------------------------------------
# Error isolation per source (R1#8)
# ---------------------------------------------------------------------------


def test_source_isolation_checklist_failure_does_not_kill_recall(session):
    """Если checklist source бросает — memory + reminder всё равно
    возвращают результаты.

    Используем разные embedding seeds (1 и 2) — иначе dedup при
    cos>0.95 справедливо схлопнёт reminder с memory как дубликат.
    """
    repo = MemoryRepository(session)
    repo.save("t1", "u1", tier="core", content="memory ok",
              embedding=_vec(1), source="user_direct")
    _add_reminder(session, rem_id="rem1", title="reminder ok",
                  embedding=_vec(2))

    with patch.object(
        repo, "_recall_checklist_source",
        side_effect=RuntimeError("simulated DB error"),
    ):
        hits, stats = repo.recall_broadcast("t1", "u1", _vec(1))

    sources = {h.source.split(":")[0] for h in hits}
    assert "memory" in sources
    assert "reminder" in sources
    # checklist source crashed → его счётчик пустой, но не падает recall
    assert stats["checklist"].count == 0


# ---------------------------------------------------------------------------
# Sanity: cross-tenant isolation (no leak)
# ---------------------------------------------------------------------------


def test_recall_broadcast_does_not_leak_across_users(session):
    """Юзер u2 в том же tenant'е не видит данные u1."""
    repo = MemoryRepository(session)
    session.add(User(id="u2", tenant_id="t1", telegram_account_id="99"))
    session.commit()
    repo.save("t1", "u1", tier="core", content="u1 secret",
              embedding=_vec(1), source="user_direct")
    _add_checklist_with_item(
        session, list_id="cl1", item_id="ci1",
        list_title="u1 list", item_title="u1 fact",
        embedding=_vec(1),
    )

    hits, _stats = repo.recall_broadcast("t1", "u2", _vec(1))
    assert hits == []
