"""Chat LLM service (Phase 3).

Thin wrapper around LangChain's ``ChatOpenAI`` pointed at an
OpenAI-compatible endpoint (primary: MiMo-V2-Pro). Returns ``None``
when no API key is configured — callers must tolerate "LLM disabled"
gracefully for dev/test scenarios where we don't want to call live
providers.

Why LangChain: tool-binding machinery (``.bind_tools([...])``) and
structured-output support are the two levers Phase 3e needs. Writing
these from scratch against raw ``httpx`` would be a week of work per
provider. Pinning to ``langchain-openai`` also means swapping
providers later (if MiMo rate-limits us) is a one-line change in
settings — no code refactor.

Parallel tool-calls: verified 2026-04-22 that MiMo-V2-Pro emits
multiple ``tool_calls`` in a single assistant message when the
prompt invites it (e.g. "что в списке И что в меню"). The
``execute_conversation_chat`` loop handles the list correctly —
saves ~1 LLM round-trip (~3-5s) on multi-read turns. No flag to
flip; behaviour is default.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from langchain_openai import ChatOpenAI

from sreda.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


# Models trained on ReAct-style tool-calling data (Gemma-4 family,
# some Qwen variants, early DeepSeek R1 forks) occasionally prepend
# reasoning-trace markers to their final user-visible text. Verified
# 2026-04-22 on google/gemma-4-26b-a4b-it: every tool-calling reply
# came back as ``thought\n<real answer>``. System prompt rules can't
# suppress it — the training signal overrides user instructions.
# Fixing it at the boundary (once, where we extract the reply text)
# keeps the rest of the code provider-agnostic.
_REASONING_PREFIXES = (
    "thought", "thinking", "reasoning", "analysis", "internal",
    "reflect", "reflection",
)
# Matches a leading ReAct marker on its own line or followed by ":".
# Case-insensitive. Example hits: ``thought\n``, ``Thinking: ``,
# ``REASONING\n\n``. The whole marker + following whitespace is
# stripped; the actual answer stays intact.
_REASONING_PREFIX_RE = re.compile(
    r"^(?:" + "|".join(_REASONING_PREFIXES) + r")\s*[:\n]\s*",
    re.IGNORECASE,
)


def strip_reasoning_prefix(text: str) -> str:
    """Remove a leading ReAct-style reasoning marker from an LLM reply.

    Returns ``text`` unchanged if no known marker is present at the
    start. Idempotent — safe to apply to already-clean output.
    """
    if not text:
        return text
    match = _REASONING_PREFIX_RE.match(text)
    if not match:
        return text
    return text[match.end():]


def get_chat_llm(
    settings: Settings | None = None,
    *,
    model: str | None = None,
    temperature: float = 0.3,
    **kwargs: Any,
) -> ChatOpenAI | None:
    """Build a ``ChatOpenAI`` instance pointed at the configured provider.

    Returns ``None`` if no API key is available (either via env or the
    fallback file). Callers should check for this and short-circuit to
    a "LLM not configured" user-facing reply rather than crash.
    """
    settings = settings or get_settings()
    api_key = settings.resolve_mimo_api_key()
    if not api_key:
        logger.info("chat LLM disabled: no MiMo API key configured")
        return None

    return ChatOpenAI(
        base_url=settings.mimo_base_url,
        api_key=api_key,
        model=model or settings.mimo_chat_model,
        temperature=temperature,
        timeout=settings.mimo_request_timeout_seconds,
        **kwargs,
    )
