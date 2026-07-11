"""Failure-taxonomy exceptions for the LLM reliability guard (#343 / F7).

Kept in a tiny dependency-free module so both ``services/llm`` and
``services/llm_bulkhead`` can import them without a circular import. Both
subclass ``Exception`` (not ``LLMCallTimeout``) so the existing
``except (LLMCallTimeout, Exception)`` fallback kernel in
``runtime/llm_caller`` still catches them and routes to the fallback provider.
"""

from __future__ import annotations


class LLMBulkheadFull(Exception):
    """Raised when the process-global LLM bulkhead is at capacity — the call is
    fast-failed WITHOUT spawning a worker thread (bounds hung-thread growth)."""


class LLMCircuitOpen(Exception):
    """Raised when a provider's circuit breaker is open — the call is
    fast-failed without touching the (hung) provider until cooldown elapses."""
