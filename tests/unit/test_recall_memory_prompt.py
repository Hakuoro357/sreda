"""Lock-in tests for the recall_memory tool docstring and the
core system prompt instructions about memory.

Background (incident 30 апреля 2026, юзер 755682022): bot answered
"only one fabric in memory" when 5+ fabric records actually existed
in `assistant_memories`. Root cause: pre-retrieval (top-k cosine)
seeded only one record into [ПАМЯТЬ] for the list-style query, and
the LLM trusted seeded [ПАМЯТЬ] as complete because both the tool
docstring and system prompt told it to call recall_memory ONLY when
the fact was missing — not when seeded data might be incomplete.

These tests don't validate runtime LLM behaviour (mock LLM only
returns what we feed it). They guard the *contracts* — the literal
strings the LLM sees — so a future refactor doesn't accidentally
restore the passive-recall wording.

Real LLM behaviour is checked by the manual smoke checklist in
``docs/qa/recall_memory_smoke.md`` after deploy.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.runtime.handlers import build_system_prompt
from sreda.runtime.tools import build_memory_tools


class _StubEmbedding:
    """Minimal EmbeddingClient stub satisfying the Protocol shape.

    Tests in this module only read tool descriptions — they never
    invoke the tools, so the stub never runs. We just need *some*
    object that `build_memory_tools` accepts."""

    def embed_query(self, text: str) -> list[float]:  # pragma: no cover
        return [0.0]

    def embed_document(self, text: str) -> list[float]:  # pragma: no cover
        return [0.0]


def _memory_tools():
    """Build memory tools bound to a throwaway in-memory tenant for
    description introspection. Tools are never executed in these tests
    — we only read ``.description``."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="Test"))
    sess.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    sess.commit()
    return {
        t.name: t
        for t in build_memory_tools(
            session=sess,
            tenant_id="t1",
            user_id="u1",
            embedding_client=_StubEmbedding(),
        )
    }


# ---------------------------------------------------------------------------
# recall_memory tool docstring contract
# ---------------------------------------------------------------------------


def test_recall_memory_docstring_has_proactive_triggers():
    """The recall_memory description must include explicit ALWAYS/BEFORE
    triggers so the LLM treats it as a proactive lookup, not a fallback.

    Regression target: the old docstring said "Use this only if you
    need to dig deeper" — passive wording that caused the 30 апреля
    fabric incident. New docstring must enumerate the cases where
    recall_memory is REQUIRED, so the LLM doesn't trust seeded
    [ПАМЯТЬ] as complete.
    """
    tools = _memory_tools()
    desc = tools["recall_memory"].description.lower()

    assert "always" in desc, (
        "recall_memory description must include 'ALWAYS' triggers — "
        "the old 'use this only if' wording caused the LLM to trust "
        "incomplete seeded [ПАМЯТЬ] in list-style queries."
    )
    assert "before" in desc, (
        "recall_memory description must include 'BEFORE' rule for "
        "negative answers — the LLM must verify with recall_memory "
        "before claiming 'у меня нет данных по X'."
    )
    # The forbidden passive-only phrasing
    assert "use this only if" not in desc, (
        "recall_memory description must NOT contain 'Use this only if' — "
        "this passive wording caused proactive-recall failures."
    )


def test_recall_memory_docstring_mentions_list_style_queries():
    """The docstring must give the LLM concrete linguistic markers for
    list-style queries (покажи все / перечисли / помнишь). Otherwise
    the LLM might recognise some triggers but miss others."""
    tools = _memory_tools()
    desc = tools["recall_memory"].description.lower()

    # Russian list-style triggers — what the user actually says.
    list_triggers_present = any(
        marker in desc
        for marker in ["покажи все", "перечисли", "помнишь", "что у меня есть"]
    )
    assert list_triggers_present, (
        "recall_memory description must mention at least one Russian "
        "list-style trigger phrase ('покажи все', 'перечисли', "
        "'помнишь', 'что у меня есть') so the LLM recognises these "
        "patterns as required-recall cases."
    )


# ---------------------------------------------------------------------------
# System prompt contract — _CORE_SYSTEM_PROMPT block in handlers.py
# ---------------------------------------------------------------------------


def test_system_prompt_recall_memory_proactive_trigger_present():
    """The core system prompt's recall_memory rule must enforce
    proactive recall on list-style queries and before negative answers.

    Regression target: the old rule said 'recall_memory — когда надо
    вытащить факт, которого нет в [ПАМЯТЬ] выше' — a passive trigger
    that did not fire on list-style queries where seeded [ПАМЯТЬ]
    contained partial-but-not-empty data.
    """
    prompt = build_system_prompt("housewife_assistant").lower()

    # Required: explicit "ОБЯЗАТЕЛЬНО при списочных запросах"
    assert "обязательно" in prompt and "списочн" in prompt, (
        "Core system prompt must instruct the LLM to call recall_memory "
        "ОБЯЗАТЕЛЬНО at списочных queries. Without this, list-style "
        "queries silently miss memories that were filtered out by the "
        "pre-retrieval top-k cutoff."
    )
    # Required: explicit "ВСЕГДА перед" negative-answer phrases
    assert "всегда" in prompt and (
        "у меня нет данных" in prompt
        or "не записывала" in prompt
        or "не помню" in prompt
    ), (
        "Core system prompt must instruct the LLM that recall_memory "
        "is ВСЕГДА required before saying 'у меня нет данных' / "
        "'не записывала' / 'не помню'. Without this, the LLM claims "
        "absence on the basis of seeded [ПАМЯТЬ] alone."
    )
    # Forbidden: the old passive wording
    assert "когда надо вытащить факт, которого нет в [память]" not in prompt, (
        "Old passive wording 'когда надо вытащить факт, которого нет "
        "в [ПАМЯТЬ]' must be removed — it caused the 30 апреля fabric "
        "incident."
    )


def test_system_prompt_anti_internals_and_anti_confabulation_present():
    """The core system prompt must contain explicit rules that:
    (a) the LLM does not explain memory internals to the user
        (top-k, retrieval, окно контекста, embedding, выборка)
    (b) the LLM does not confabulate retrospectives about its own
        previous actions inside the session.

    Regression target: 1 мая 11:57 the bot wrote to a real user
    'передаётся ограниченное количество самых релевантных записей'
    and 'окно контекста' — leaking implementation details. And in
    11:56 it confabulated a sequence of internal actions that did
    not occur.
    """
    prompt = build_system_prompt("housewife_assistant").lower()

    # (a) anti-internals
    assert "не объясняй" in prompt and "память" in prompt, (
        "Core system prompt must forbid the LLM from explaining "
        "memory internals to the user. Users complained when the "
        "bot leaked phrases like 'top-k retrieval', 'окно контекста'."
    )
    # The bot must specifically be told NOT to use these jargon words
    forbidden_jargon_marked_in_prompt = all(
        word in prompt
        for word in ["top-k", "retrieval", "embedding"]
    )
    # We expect them mentioned ONLY in the context of "do not say".
    # If they appear, they must be co-located with a negation marker.
    if forbidden_jargon_marked_in_prompt:
        # Find the negation block — the prompt should explicitly list
        # them under "никаких слов про" or similar.
        assert "никаких слов" in prompt or "не используй" in prompt, (
            "Core system prompt mentions internal jargon (top-k, "
            "retrieval, embedding) — but only as a NEGATIVE list. "
            "Make sure the negation marker ('никаких слов про', "
            "'не используй') is present near them."
        )

    # (b) anti-confabulation
    assert "не выдумывай" in prompt or "не сочиняй" in prompt, (
        "Core system prompt must forbid the LLM from confabulating "
        "retrospective accounts of its own actions inside the "
        "session. Users see invented step sequences and lose trust."
    )
    assert (
        "не отслеживаю" in prompt
        or "настолько точно" in prompt
        or "проверить заново" in prompt
    ), (
        "Core system prompt must give the LLM an honest fallback "
        "phrase for when the user asks 'why did you do X then Y' — "
        "e.g. 'не отслеживаю свои шаги настолько точно, могу "
        "проверить заново'. Otherwise the LLM fills the gap with "
        "fabricated narrative."
    )
