"""Strict envelope-stripping JSON parser for PLANNER output (#113).

The planner LLM occasionally wraps its JSON plan in a ``` ```json … ``` ```
markdown fence despite the prompt forbidding it (planner_v1.md). This parser
strips a SINGLE outer fence (plus a BOM / surrounding whitespace) before
``json.loads``, so a fenced-but-otherwise-valid plan is accepted instead of
wasting a retry or terminating as invalid.

STRICT by design (Codex consult, #110 Phase-6 follow-up):

* Accepted: a leading BOM / whitespace; exactly ONE outer fence
  (``` ```json ```, ``` ```JSON ```, or a bare ``` ``` ```) with a REQUIRED
  matching closing fence and nothing but the JSON inside.
* NOT accepted → raises ``json.JSONDecodeError`` (identical to a naive
  ``json.loads`` failure, so the caller's existing retry / terminate path
  fires): prose before or after the fence, an opening fence with no closing
  fence, or genuinely malformed JSON.
* This is NOT a tolerant "find the first ``{...}``" extractor — that would mask
  real malformations and risk parsing the wrong object. Planner-only; do not
  reuse it as a general LLM-output extractor.

Used by BOTH the production orchestrator and the offline replay harness so they
parse planner output identically.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Observability: how often an outer fence was stripped. The prompt says "no
# markdown", so every strip is a planner-compliance miss we must still SEE —
# once we accept fences, the retry no longer "trains" the model away from them.
# BEST-EFFORT in-process counter (not thread/process-safe, may lose increments
# under concurrency, needs a per-test reset). The ``planner_output_fence_stripped``
# log line below is the DURABLE signal for real aggregation (Codex #113 R1).
FENCE_METRICS: dict[str, int] = {"fence_stripped": 0}

# One outer fence: ```[json]\n <body> \n``` — STRICT (Codex #113 R1, both A/B):
#   * info string is ONLY bare or ``json``/``JSON`` (a ```python-wrapped plan
#     stays a VISIBLE compliance miss, not a silent accept);
#   * a REQUIRED newline after the fence-info line (so inline ```json{...} with
#     no delimiter is rejected, never half-parsed);
#   * REQUIRED closing fence, WHOLE string is the block (anchored ^…$ → no prose
#     before/after); non-greedy body; DOTALL so the JSON may span lines.
_FENCE_RE = re.compile(
    r"^```(?:[Jj][Ss][Oo][Nn])?[ \t]*\r?\n(?P<body>.*?)\r?\n?```$", re.DOTALL
)


def parse_planner_json(raw_text: str) -> Any:
    """Parse a planner JSON response, stripping at most ONE outer markdown fence.

    Raises ``json.JSONDecodeError`` on anything that is not clean JSON (or a
    clean single-fence-wrapped clean JSON) — callers already handle that."""
    # Strip outer whitespace, then a leading BOM, then whitespace again — so
    # both "﻿…" and " ﻿…" normalize regardless of order (Codex #113 R1).
    s = raw_text.strip().lstrip(chr(0xFEFF)).strip()
    if s.startswith("```"):
        m = _FENCE_RE.match(s)
        if m is not None:
            s = m.group("body").strip()
            FENCE_METRICS["fence_stripped"] += 1
            logger.info("planner_output_fence_stripped")
        # else: an opening ``` without a clean closing fence (or prose around
        # it) — leave ``s`` unchanged so ``json.loads`` below raises and the
        # caller's retry / terminate path handles it (we never half-strip).
    return json.loads(s)
