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


def _log_memory_date_drift(
    content: str, op_name: str, tenant_id: str, user_id: str,
) -> None:
    """Pre-save guard: log WARNING if memory content references past dates.

    Same class бага что post-output date drift (см. handlers.py Layer 2b).
    Prod incident 2026-05-18: bot записал summary с «17.05.2026, утро —
    сахар 10.2» когда реально 18 мая. Если drift detected — flag в логе
    для admin review.

    Не блокируем save: drift может быть legitimate ("вчера записала
    давление") когда user явно ссылается на past. Без access к user_text
    в memory tool scope мы не можем carve-out — поэтому log-only.
    Реальный fix для измерений требует separate structured table; пока
    memory tools принимают free-form text, ограничиваемся observability.
    """
    try:
        from datetime import datetime, timezone
        from sreda.services.date_drift_validator import find_drifted_dates
        # Use UTC date as approximation (TZ edge case при 00:00-02:59 MSK
        # минорный — acceptable trade-off для logging only).
        iso_today = datetime.now(timezone.utc).date().isoformat()
        drifted = find_drifted_dates(
            content, iso_date_today=iso_today, user_text="",
        )
        if drifted:
            logger.warning(
                "MEMORY_DATE_DRIFT op=%s tenant=%s user=%s today=%s "
                "findings=%s",
                op_name, tenant_id, user_id, iso_today,
                [f"{d['raw']}({d['days_ago']}d ago)" for d in drifted],
            )
    except Exception:
        logger.exception("memory date drift check failed — skipping")


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
        _log_memory_date_drift(text, "save_core_fact", tenant_id, user_id)
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
        _log_memory_date_drift(text, "save_episode", tenant_id, user_id)
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
        """Search what the user previously told you — across memory,
        active checklists, and pending reminders (recall-broadcast,
        2026-05-04).

        ALWAYS call this tool when:
        - the user asks for a list/all/everything about a topic
          ("покажи все Х", "что у меня есть про Y", "перечисли все Z",
          "помнишь Y", "в переписке было")
        - you can't find a specific fact in [ПАМЯТЬ] above and want to
          verify before answering
        - BEFORE saying "у меня нет данных по X" / "ты не записывала Y" /
          "я этого не помню" — first call recall_memory with relevant
          query, then decide. Never claim absence without verifying.

        Search covers three stores:
        - core/episodic memory (``source="memory:core"`` /
          ``"memory:episodic"``) — long-term curated facts
        - active checklist items (``source="checklist:<id>"``,
          ``metadata.list_title``) — items the user dictated into a
          named list
        - pending reminders (``source="reminder:<id>"``,
          ``metadata.due_at``) — scheduled triggers

        At least one hit from each source with matches above min_score
        is included regardless of global rank (``min_per_source=1``
        guarantee). When you cite a hit to the user, reference its
        source naturally ("у тебя в чек-листе «План кроя»: ..." for
        checklist; "в напоминании на 18 мая: ..." for reminder). NEVER
        say "это в core памяти" / "запомнила в long-term store" —
        источник для юзера называть на русском по смыслу, не техжаргоном.

        The conversation handler seeds memories into your prompt at the
        start of each turn, but that seed is memory-only and may miss
        checklist/reminder content; verify via this tool when needed.

        Args:
            query: the question or topic to recall. Use either the user's
                   exact phrasing OR specific keywords from it
                   ("ткани характеристики", "дети возраст", "адреса").
            top_k: number of results to return (default 3, max 10). Bump
                   to 10 for list-style queries; default 3 is fine for
                   single-fact lookups.

        Returns:
            JSON string: ``[{content, source, score, metadata}, ...]``.
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
        hits, _stats = repo.recall_broadcast(
            tenant_id, user_id, query_vec,
            top_k=top_k, min_score=0.1,
        )
        return json.dumps(
            [
                {
                    "content": hit.content,
                    "source": hit.source,
                    "score": round(hit.score, 3),
                    "metadata": hit.metadata,
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

    # #200 Фаза 1: web_search доступен ВСЕМ. Тир определяет per-user
    # лимит: free (sreda_free и НЕ grandfathered) — 5/мес (детерминир.
    # атомарный счётчик); grandfathered/платные — без per-user лимита
    # (глобальный 950 остаётся). Enforcement в замыкании; апсейл —
    # машинный статус, LLM формулирует.
    from sreda.services.entitlement_gate import EntitlementGate
    _gate = EntitlementGate(session).check(tenant_id)
    _is_free_only = (
        _gate.plan_key == "sreda_free" and not _gate.is_grandfathered
    )
    _per_user_cap = 5 if _is_free_only else None

    web_search_tool = build_web_search_tool(
        session=session, tenant_id=tenant_id, user_id=user_id,
        per_user_cap=_per_user_cap, is_free=_is_free_only,
    )
    # #244: fetch_url через выделенный фильтр-egress + per-day квота (no-refund).
    # Контекст симметрично web_search; is_free тот же гейт. per_day/per_turn/proxy
    # резолвятся из настроек внутри build_fetch_url_tool (None → settings).
    fetch_url_tool = build_fetch_url_tool(
        session=session, tenant_id=tenant_id, user_id=user_id,
        is_free=_is_free_only,
    )
    weather_tool = build_weather_tool()

    tools_list: list[Callable] = [
        save_core_fact,
        save_episode,
        recall_memory,
        weather_tool,
        web_search_tool,
        fetch_url_tool,
        log_unsupported_request,
    ]
    return tools_list
