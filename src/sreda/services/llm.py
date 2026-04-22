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


# Supported chat-LLM providers. Extend this tuple AFTER adding a build
# branch in ``_build_chat_llm`` and a matching setting block in
# ``config.settings``; the handler layer treats unknown providers as
# "LLM disabled" rather than crashing the turn.
CHAT_PROVIDERS = ("mimo", "openrouter")


def _build_chat_llm(
    provider: str,
    settings: Settings,
    *,
    model: str | None,
    temperature: float,
    **kwargs: Any,
) -> ChatOpenAI | None:
    """Construct a ``ChatOpenAI`` for the named provider or return None
    if the provider isn't configured (missing key, unknown name).

    Keeping the per-provider wiring in one helper lets
    ``get_chat_llm`` stay small and makes adding a provider a single
    new ``if`` branch.
    """
    if provider == "mimo":
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
    if provider == "openrouter":
        api_key = settings.resolve_openrouter_api_key()
        if not api_key:
            logger.info("chat LLM disabled: no OpenRouter API key configured")
            return None
        return ChatOpenAI(
            base_url=settings.openrouter_base_url,
            api_key=api_key,
            model=model or settings.openrouter_chat_model,
            temperature=temperature,
            timeout=settings.mimo_request_timeout_seconds,
            **kwargs,
        )
    logger.warning("chat LLM: unknown provider %r — ignoring", provider)
    return None


# Once-per-process flag so a persistent DB problem (missing table,
# revoked permissions) doesn't spam a full traceback on every chat
# turn. First hit logs at WARNING with the cause; subsequent hits
# are silent until the process restarts.
_RUNTIME_CONFIG_WARNED = False


def _resolve_provider_overrides(settings: Settings) -> tuple[str, str | None]:
    """Consult the admin-switcher DB table for live overrides, falling
    back to env-var-based Settings when a key isn't set. Returns
    ``(primary, fallback_or_None)``.

    An empty-string override in the DB is treated as "explicitly
    disable" — useful when the admin wants to kill the fallback chain
    without nulling the setting entirely.

    DB errors (missing table, locked db, etc.) degrade silently to
    env defaults so a half-migrated install still serves turns.
    """
    global _RUNTIME_CONFIG_WARNED

    primary = settings.chat_provider
    fallback = settings.chat_fallback_provider
    try:
        from sqlalchemy.exc import OperationalError, ProgrammingError

        from sreda.db.session import get_session_factory
        from sreda.services import runtime_config as rc
    except ImportError:
        return primary, fallback

    try:
        session = get_session_factory()()
    except Exception:  # noqa: BLE001 — session factory not ready yet
        return primary, fallback
    try:
        db_primary = rc.get_config(session, rc.KEY_CHAT_PROVIDER)
        db_fallback = rc.get_config(session, rc.KEY_CHAT_FALLBACK_PROVIDER)
    except (OperationalError, ProgrammingError) as exc:
        if not _RUNTIME_CONFIG_WARNED:
            logger.warning(
                "chat LLM: runtime_config unavailable (%s) — using env defaults; "
                "create the table via Base.metadata.create_all to enable the "
                "admin LLM-switcher. This warning fires once per process.",
                type(exc).__name__,
            )
            _RUNTIME_CONFIG_WARNED = True
        return primary, fallback
    finally:
        session.close()

    if db_primary:
        primary = db_primary
    if db_fallback is not None:
        # Empty string = explicit "no fallback".
        fallback = db_fallback or None
    return primary, fallback


def get_chat_llm(
    settings: Settings | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    temperature: float = 0.3,
    with_fallback: bool = False,
    **kwargs: Any,
) -> Any | None:
    """Build a chat-LLM client pointed at the configured provider.

    Returns ``None`` when no provider is available (no key configured,
    or an unknown provider name). Callers must tolerate this gracefully
    and short-circuit the turn with a "LLM disabled" reply — crashing
    hurts UX more than admitting the limitation.

    Resolution order for the provider name:
      1. Explicit ``provider=`` argument (bench/probe use).
      2. ``runtime_config.chat_primary_provider`` (admin live-switch).
      3. ``settings.chat_provider`` (env-var / default).

    Parameters
    ----------
    provider :
        Override for the resolved provider. Skips the admin-switcher
        DB lookup entirely — intended for bench tools and hot-probing
        a specific backend without mutating persistent state.
    with_fallback :
        When True and a fallback provider is configured (via admin
        switcher or env), wraps the primary runnable with LangChain's
        ``.with_fallbacks([...])``. The fallback kicks in on ANY
        exception from the primary — rate limits, timeouts, 5xx — and
        replays the same message list against the backup transparently.
        One level deep on purpose; three-tier would hide the freshly-
        interesting failure mode.
    """
    settings = settings or get_settings()
    if provider is not None:
        effective_primary = provider
        effective_fallback = None  # bench/probe: fallback irrelevant
    else:
        effective_primary, effective_fallback = _resolve_provider_overrides(settings)
    primary_llm = _build_chat_llm(
        effective_primary, settings,
        model=model, temperature=temperature, **kwargs,
    )
    if primary_llm is None:
        return None
    if not with_fallback or not effective_fallback:
        return primary_llm
    if effective_fallback == effective_primary:
        # Fallback same as primary is a no-op and a config smell.
        logger.warning(
            "chat LLM: fallback provider equals primary (%s) — skipping wrap",
            effective_primary,
        )
        return primary_llm
    fallback_llm = _build_chat_llm(
        effective_fallback, settings,
        model=None,  # fallback uses its provider's default model
        temperature=temperature, **kwargs,
    )
    if fallback_llm is None:
        logger.warning(
            "chat LLM: fallback provider %r not configured — primary-only",
            effective_fallback,
        )
        return primary_llm
    logger.info(
        "chat LLM: wrapping %s with fallback → %s",
        effective_primary, effective_fallback,
    )
    return primary_llm.with_fallbacks([fallback_llm])
