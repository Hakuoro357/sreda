"""Credit consumption formula (Phase 4.5).

Maps LLM token usage to MiMo credits for budget accounting. The rates
come straight from the MiMo pricing doc:

  * mimo-v2-omni         → 1x  (reference rate, includes reasoning tokens)
  * mimo-v2-pro  <256k   → 2x
  * mimo-v2-pro  256k-1M → 4x
  * mimo-v2-tts          → 0   (free during beta)

Total credits = (prompt_tokens + completion_tokens) * rate. This is our
platform-side estimate; MiMo's own billing is authoritative. We use this
number for quota enforcement and ``/stats`` display.
"""

from __future__ import annotations


CONTEXT_256K = 256_000


def credits_for(
    model: str, prompt_tokens: int, completion_tokens: int
) -> int:
    total = max(0, prompt_tokens) + max(0, completion_tokens)
    if total == 0:
        return 0
    key = (model or "").strip().lower()
    if key == "mimo-v2-tts":
        return 0
    if key == "mimo-v2-omni":
        return total  # 1x
    if key == "mimo-v2-pro":
        if prompt_tokens >= CONTEXT_256K:
            return total * 4  # 4x in the 256k-1M tier
        return total * 2  # 2x below 256k
    # Unknown model: fall back to the most pessimistic rate so we don't
    # under-count. Operators see an anomaly in /stats and can add a rate
    # for the new model.
    return total * 2
