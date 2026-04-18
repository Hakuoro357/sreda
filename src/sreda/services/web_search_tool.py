"""Web search LLM tool — DuckDuckGo Instant Answers via duckduckgo-search.

Chosen over Tavily/SerpAPI because it needs no API key, which matches
Среда's MVP posture (no third-party secrets in the critical path for a
beta-phase skill). Rate limits on DDG are generous for ≤1 req/turn;
if they ever bite, we can swap the provider here without changing
LLM-facing surface.

Failures are returned as short error strings the LLM reads and adapts
to — never as exceptions that would kill the whole chat turn.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from langchain_core.tools import tool as lc_tool

logger = logging.getLogger(__name__)

_MAX_RESULTS = 3
_MAX_SNIPPET_CHARS = 280
_REGION = "ru-ru"  # Russian-first, matches the product audience


def build_web_search_tool() -> Callable:
    """Return a LangChain tool the chat LLM can call with ``query`` str.

    Declared as a factory for parity with ``build_memory_tools`` —
    future versions may bind per-tenant config (region, safesearch,
    quota) here without changing call sites.
    """

    @lc_tool
    def web_search(query: str) -> str:
        """Search the public web and return the top 3 results.

        Use when you need fresh info beyond what's stored in memory:
        news, current events, specific facts you don't know,
        user-facing phrases/definitions, schedules that change often.
        Do NOT use for private user data (call ``recall_memory`` for
        that) or for settled facts you already know.

        Args:
            query: Short search phrase. Write it as you'd type into
                Google — 3-8 words, no quotes unless exact match is
                critical.

        Returns:
            A formatted block with up to 3 results, each
            "N. Title\\n<snippet>\\n<url>". Returns a short error
            string on failure; adapt and respond gracefully.
        """
        q = (query or "").strip()
        if not q:
            return "error: empty query"
        try:
            # Lazy import — keeps the dependency cost off the hot path
            # for skills that never touch web search.
            from duckduckgo_search import DDGS  # type: ignore
        except ImportError:
            logger.warning("duckduckgo-search not installed")
            return "error: web_search not available"

        try:
            with DDGS() as ddgs:
                raw = list(ddgs.text(q, region=_REGION, max_results=_MAX_RESULTS))
        except Exception as exc:  # noqa: BLE001 — network / library errors
            logger.warning("web_search failed for %r: %s", q, exc)
            return f"error: {type(exc).__name__}"

        if not raw:
            return "no results"

        lines: list[str] = []
        for idx, item in enumerate(raw[:_MAX_RESULTS], start=1):
            title = (item.get("title") or "").strip()
            body = (item.get("body") or "").strip()
            url = (item.get("href") or item.get("url") or "").strip()
            if len(body) > _MAX_SNIPPET_CHARS:
                body = body[:_MAX_SNIPPET_CHARS].rstrip() + "…"
            lines.append(f"{idx}. {title}\n{body}\n{url}")
        return "\n\n".join(lines)

    return web_search
