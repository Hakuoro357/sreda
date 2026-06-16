"""Planner LLM client wrapper — typed result, timeout-safe.

Sub-A12 Phase B.2.

Thin wrapper over ``services.llm.get_chat_llm`` + ``invoke_with_per_call_timeout``.
Returns a structured ``PlannerCallResult`` so callers (Phase B.4 orchestrator)
can record raw_text, latency, model, provider, and attempt metadata into
``planner_executions`` without juggling LangChain response shapes.

Parsing & validation are NOT in this module — those live in the orchestrator
(Phase B.4) so this layer stays a one-shot LLM call with a clean contract.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from sreda.config.settings import Settings, get_settings
from sreda.services.llm import (
    LLMCallTimeout,
    get_chat_llm,
    invoke_with_per_call_timeout,
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PlannerProviderUnavailable(RuntimeError):
    """Raised when ``get_chat_llm`` returns None for the configured planner
    provider (key not set, or unknown provider name). Caller maps to
    ``planner_status='invalid'`` + ``failure_kind='provider_error'``.
    """


class PlannerTimeoutError(LLMCallTimeout):
    """Re-export of the LLM-level timeout under a planner-namespaced name
    so orchestrator code can catch ``PlannerTimeoutError`` without
    leaking the services.llm namespace upward. Inherits ``LLMCallTimeout``
    so existing ``except LLMCallTimeout`` paths still match.
    """


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannerCallResult:
    """One LLM call's full transcript + metadata.

    Codex Sub-A12 R1/R2 MAJOR (high catch): callers need the raw text
    PLUS provider/model/latency/attempt context to populate
    ``planner_executions`` and the ``#68`` LLM trace correlation. A
    bare ``(raw, latency)`` tuple loses too much information."""

    raw_text: str
    latency_ms: int
    provider: str
    model: str
    attempt_no: int
    # parsed_plan stays None at this layer — parsing lives in the
    # orchestrator (Phase B.4) so retry/validation logic owns the JSON
    # parse error path consistently.
    parsed_plan: Any = None
    # F-1 (#151): token usage from the LLM response so the orchestrator can
    # sum across attempts and the chat loop can record real planner spend
    # into skill_ai_executions. 0 when the provider omits usage_metadata.
    prompt_tokens: int = 0
    completion_tokens: int = 0


# ---------------------------------------------------------------------------
# Public call
# ---------------------------------------------------------------------------


def _default_settings_factory() -> Settings:
    """Indirection so tests can substitute settings without import-order
    surgery. Returns whatever the current ``get_settings()`` cache holds."""
    return get_settings()


def call_planner(
    prompt: str,
    *,
    provider: str | None = None,
    timeout_seconds: float | None = None,
    attempt_no: int = 1,
    settings_factory: Callable[[], Settings] = _default_settings_factory,
    chat_llm_factory: Callable[..., Any] | None = None,
    invoke: Callable[..., Any] | None = None,
    now_ms: Callable[[], int] | None = None,
) -> PlannerCallResult:
    """Send ``prompt`` to the planner LLM, return typed result.

    Parameters
    ----------
    prompt :
        Fully-built prompt text (cached prefix + variable suffix
        rendered by ``runtime.planner.prompt_builder.build_prompt``).
        Wrapped in a single LangChain ``HumanMessage`` for invoke.
    provider :
        Override the configured planner provider. Default uses
        ``settings.planner_provider`` (= ``"mimo-v2.5-pro"`` → model
        mimo-v2.5-pro; provider key == model name after the 2026-05-29
        rename).
    timeout_seconds :
        Wall-clock cap for the LLM call. Default uses
        ``settings.planner_timeout_sec`` (60). Hard timeout via
        ``invoke_with_per_call_timeout``.
    attempt_no :
        1 = first try, 2 = retry-after-invalid. Echoed in result for
        the orchestrator's attempt log; this function does NOT
        implement retry — that's orchestrator policy.
    settings_factory / chat_llm_factory / invoke / now_ms :
        Test-injection hooks. Production paths use defaults.

    Returns
    -------
    PlannerCallResult :
        raw_text + latency + provider/model/attempt context.

    Raises
    ------
    PlannerProviderUnavailable :
        Configured provider has no key / is unknown.
    PlannerTimeoutError :
        Wall-clock cap exceeded. Subclasses ``LLMCallTimeout`` so
        existing catch-paths still match.
    Exception :
        Any other provider-side error is propagated untouched —
        orchestrator decides how to map.
    """
    s = settings_factory()
    effective_provider = provider or s.planner_provider
    effective_timeout = (
        timeout_seconds if timeout_seconds is not None else s.planner_timeout_sec
    )
    factory = chat_llm_factory or get_chat_llm
    invoke_fn = invoke or invoke_with_per_call_timeout
    clock = now_ms or (lambda: int(time.monotonic() * 1000))

    runnable = factory(settings=s, provider=effective_provider)
    if runnable is None:
        raise PlannerProviderUnavailable(
            f"get_chat_llm returned None for provider={effective_provider!r} "
            f"— key not configured or provider name unknown"
        )

    # Import HumanMessage lazily so test/CI without langchain-core full
    # stack can still import this module for shape checks. Production
    # path always has langchain installed.
    from langchain_core.messages import HumanMessage

    messages = [HumanMessage(content=prompt)]

    started = clock()
    try:
        response = invoke_fn(
            runnable, messages, timeout_seconds=effective_timeout,
        )
    except LLMCallTimeout as exc:
        # Re-wrap into planner namespace. Original cause preserved.
        raise PlannerTimeoutError(str(exc)) from exc
    elapsed_ms = clock() - started

    raw_text = _extract_text(response)
    model_name = _resolve_model_name(runnable, s, effective_provider)
    prompt_tokens, completion_tokens = _extract_usage(response)

    return PlannerCallResult(
        raw_text=raw_text,
        latency_ms=elapsed_ms,
        provider=effective_provider,
        model=model_name,
        attempt_no=attempt_no,
        parsed_plan=None,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


# ---------------------------------------------------------------------------
# Helpers (private)
# ---------------------------------------------------------------------------


def _extract_usage(response: Any) -> tuple[int, int]:
    """Pull (prompt_tokens, completion_tokens) from a LangChain AIMessage's
    ``usage_metadata`` (input_tokens/output_tokens). Returns (0, 0) when the
    provider omits usage — never raises (F-1 #151, mirrors handlers.py:2235)."""
    usage = getattr(response, "usage_metadata", None) or {}
    try:
        prompt = int(usage.get("input_tokens") or 0)
        completion = int(usage.get("output_tokens") or 0)
    except (AttributeError, TypeError, ValueError):
        return 0, 0
    return max(prompt, 0), max(completion, 0)


def _extract_text(response: Any) -> str:
    """Pull plain string content out of a LangChain AIMessage (or fall
    back to ``str(response)`` for test stubs/dicts)."""
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Multimodal-style content blocks; concatenate text parts.
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text_val = item.get("text") or item.get("content")
                if isinstance(text_val, str):
                    parts.append(text_val)
        return "".join(parts)
    if isinstance(response, str):
        return response
    return str(response)


def _resolve_model_name(runnable: Any, settings: Settings, provider: str) -> str:
    """Best-effort: pick the actual model string. LangChain chat
    clients usually expose ``.model_name`` or ``.model``; fall back to
    the provider key when the runnable doesn't reveal a model name
    (e.g. wrapped with .with_fallbacks() — outermost handle is opaque).

    R2 fix (Codex MINOR): use real symbol names from services/llm.py
    (``_MIMO_MODEL_BY_PROVIDER`` / ``_OPENROUTER_MODEL_BY_PROVIDER``).
    Earlier code referenced ``_PROVIDER_TO_MODEL`` which never existed,
    so the fallback was dead — opaque runnables wrote ``provider`` as
    model name instead of the actual model literal.
    """
    for attr in ("model_name", "model", "model_id"):
        value = getattr(runnable, attr, None)
        if isinstance(value, str) and value:
            return value
    # services/llm.py provider→model alias maps are the next-best fallback.
    # One map per provider family (mimo / openrouter / inception) — needed
    # only for opaque runnables (.with_fallbacks()) that hide .model_name.
    try:
        from sreda.services.llm import (  # type: ignore[attr-defined]
            _INCEPTION_MODEL_BY_PROVIDER,
            _MIMO_MODEL_BY_PROVIDER,
            _OPENROUTER_MODEL_BY_PROVIDER,
        )
        for table in (
            _MIMO_MODEL_BY_PROVIDER,
            _OPENROUTER_MODEL_BY_PROVIDER,
            _INCEPTION_MODEL_BY_PROVIDER,
        ):
            if isinstance(table, dict):
                mapped = table.get(provider)
                if isinstance(mapped, str) and mapped:
                    return mapped
    except Exception:  # noqa: BLE001 — defensive
        pass
    return provider


__all__ = [
    "PlannerCallResult",
    "PlannerProviderUnavailable",
    "PlannerTimeoutError",
    "call_planner",
]
