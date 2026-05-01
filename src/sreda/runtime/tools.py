"""LLM tools exposed to the conversation agent (Phase 3).

Tools are plain functions that the LLM can call via structured output.
The conversation handler binds the LLM to these tools, loops on
``.tool_calls`` until the model stops requesting them, and returns the
final assistant message to the user.

Scope (MVP):
  * ``save_core_fact``  — persist a stable user fact (core memory tier)
  * ``save_episode``    — persist a short-term event summary (episodic)
  * ``recall_memory``   — do an ad-hoc similarity search (the handler
                          already seeded top-k into the prompt, but the
                          LLM might want to dig deeper mid-response)

Each tool is built as a **factory** that captures the session +
repo + embedding client in a closure. This keeps the signatures the LLM
sees minimal (``save_core_fact(content: str)`` — the ``tenant_id`` /
``user_id`` leak would confuse the model) and contains all side-effects
behind a clean boundary.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from langchain_core.tools import tool as lc_tool
from sqlalchemy.orm import Session

from sreda.db.repositories.memory import MemoryRepository
from sreda.services.embeddings import EmbeddingClient

logger = logging.getLogger(__name__)


def build_memory_tools(
    *,
    session: Session,
    tenant_id: str,
    user_id: str,
    embedding_client: EmbeddingClient,
) -> list[Callable]:
    """Build the three memory tools, bound to a specific user context.

    Returns a list of LangChain tool objects the caller passes to
    ``llm.bind_tools([...])``. Each tool commits on its own — we don't
    want a single bad tool call to roll back earlier successful saves
    from the same turn."""
    repo = MemoryRepository(session)

    @lc_tool
    def save_core_fact(content: str) -> str:
        """Save a stable long-term fact about the user (core memory tier).

        Use ONLY for durable truths that will remain valid across sessions
        — family, work, location, long-term preferences. NOT for moods,
        transient events, or opinions that might change next week.

        Args:
            content: the fact in a single concise sentence, preserving
                the user's own wording where possible.
        """
        text = (content or "").strip()
        if not text:
            return "error: empty content"
        try:
            embedding = embedding_client.embed_document(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("save_core_fact: embedding failed: %s", exc)
            embedding = None
        row = repo.save(
            tenant_id,
            user_id,
            tier="core",
            content=text,
            embedding=embedding,
            source="agent_inferred",
        )
        session.commit()
        return f"saved_core:{row.id}"

    @lc_tool
    def save_episode(summary: str) -> str:
        """Save a short-term event or conversation summary (episodic memory).

        Use for recent events, feelings, or context that helps you recall
        "what's been happening lately" without cluttering the stable
        fact store. Summaries are discarded over time; use ``save_core_fact``
        if the thing is actually durable.

        Args:
            summary: 1-2 sentence summary of the event or state.
        """
        text = (summary or "").strip()
        if not text:
            return "error: empty summary"
        try:
            embedding = embedding_client.embed_document(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("save_episode: embedding failed: %s", exc)
            embedding = None
        row = repo.save(
            tenant_id,
            user_id,
            tier="episodic",
            content=text,
            embedding=embedding,
            source="agent_inferred",
        )
        session.commit()
        return f"saved_episode:{row.id}"

    @lc_tool
    def recall_memory(query: str, top_k: int = 3) -> str:
        """Search previously saved user memories by semantic similarity.

        ALWAYS call this tool when:
        - the user asks for a list/all/everything about a topic
          ("покажи все Х", "что у меня есть про Y", "перечисли все Z",
          "помнишь Y", "в переписке было")
        - you can't find a specific fact in [ПАМЯТЬ] above and want to
          verify before answering
        - BEFORE saying "у меня нет данных по X" / "ты не записывала Y" /
          "я этого не помню" — first call recall_memory with relevant
          query, then decide. Never claim absence without verifying.

        The conversation handler seeds memories into your prompt at the
        start of each turn, but that seed may be incomplete for
        list-style queries. Don't assume seeded [ПАМЯТЬ] is the full
        picture — when the user asks for completeness, verify.

        Args:
            query: the question or topic to recall. Use either the user's
                   exact phrasing OR specific keywords from it
                   ("ткани характеристики", "дети возраст", "адреса").
            top_k: number of results to return (default 3, max 10). Bump
                   to 10 for list-style queries; default 3 is fine for
                   single-fact lookups.

        Returns:
            JSON string with list of {content, tier, score}.
        """
        q = (query or "").strip()
        if not q:
            return json.dumps([])
        top_k = max(1, min(int(top_k), 10))
        try:
            query_vec = embedding_client.embed_query(q)
        except Exception as exc:  # noqa: BLE001
            logger.warning("recall_memory: embedding failed: %s", exc)
            return json.dumps([])
        hits = repo.recall(tenant_id, user_id, query_vec, top_k=top_k)
        return json.dumps(
            [
                {
                    "content": hit.memory.content,
                    "tier": hit.memory.tier,
                    "score": round(hit.score, 3),
                }
                for hit in hits
            ],
            ensure_ascii=False,
        )

    feature_requests_logger = logging.getLogger("sreda.feature_requests")

    @lc_tool
    def log_unsupported_request(user_asked: str, reason: str) -> str:
        """Log a user request the assistant CANNOT fulfil right now.

        Call whenever the user asks for something the assistant lacks
        the skill, tool, integration, or data to do — so product can
        see real hotspots and prioritize. Examples: "закажи такси",
        "напиши код для X", "позвони в банк", "купи билеты".

        Do NOT call for things you CAN do via existing tools (schedule
        reminders, recall memory, web search, save facts). Do NOT call
        as a cop-out when you just need to think harder.

        Args:
            user_asked: A short paraphrase of the user's ask, in the
                user's language, ≤ 200 chars.
            reason: Why it's unsupported — which skill / integration /
                tool is missing. Concrete ("нет интеграции с Яндекс.Такси")
                beats vague ("not supported").

        Returns "ok:logged" on success. Does not surface anything to
        the user — your job after calling it is to reply to the user
        gracefully (explain briefly, suggest a workaround if you have one).
        """
        asked = (user_asked or "").strip()[:200]
        why = (reason or "").strip()[:200]
        if not asked or not why:
            return "error: both user_asked and reason required"
        feature_requests_logger.info(
            "tenant=%s user=%s asked=%r reason=%r",
            tenant_id,
            user_id,
            asked,
            why,
        )
        return "ok:logged"

    # web tools are imported lazily so skills that don't expose them
    # (or environments without duckduckgo-search / readability-lxml
    # installed) don't pay the import cost. The factory calls are
    # cheap — just closures around httpx/ddgs clients.
    from sreda.services.web_search_tool import (
        build_fetch_url_tool,
        build_web_search_tool,
    )
    # 2026-04-29: get_weather через Open-Meteo (free, без API key,
    # 14-day forecast). Заменяет fetch_url(wttr.in) для погодных
    # запросов — wttr отдавал только current weather, прогноз
    # фактически не работал.
    from sreda.services.weather_tool import build_weather_tool

    # 2026-04-29: web_search получает session/tenant/user для quota
    # tracking (Tavily 30/user/мес + 950 global). При исчерпании квоты
    # tool сам fall'нётся на DDG `backend="api"`.
    web_search_tool = build_web_search_tool(
        session=session, tenant_id=tenant_id, user_id=user_id,
    )
    fetch_url_tool = build_fetch_url_tool()
    weather_tool = build_weather_tool()

    return [
        save_core_fact,
        save_episode,
        recall_memory,
        weather_tool,
        web_search_tool,
        fetch_url_tool,
        log_unsupported_request,
    ]
