"""Canonical ``okv2:`` wire codec for typed tool outputs (#115).

Background. Write-tools must return AFFECTED ITEMS BY NAME (the epic #74
deliverable that Sub-A4 dropped). Names contain ``:``, ``,`` and spaces, so the
legacy positional ``ok:<status>:<segments>`` format cannot carry them safely.
#115 introduces a versioned envelope that wraps a JSON payload:

    okv2:<status>:<json_payload>

where ``<status>`` is the Pydantic discriminator literal of the tool's
output-model variant and ``<json_payload>`` is a JSON object carrying the
by-name fields (e.g. ``{"added_count": 2, "created": ["молоко", "хлеб"]}``).

Design (locked across plan rounds R1-R5, Codex high+medium + Kimi):

* **Only migrated tools** emit ``okv2:``. Legacy ``ok:``/``error:`` strings are
  untouched and continue through their existing positional parsers.
* Detection is a literal prefix match (``raw.startswith("okv2:")``) — never a
  ``try/except`` heuristic.
* The envelope is validated through :class:`ToolOkV2Envelope`: exactly three
  ``split(":", 2)`` segments, non-empty payload segment, ``json.loads`` of a
  JSON **object**, ``status`` in the caller's ``accepted_statuses``, and the
  payload MUST NOT carry a reserved ``status`` key (Kimi R4: a payload-level
  ``status`` would otherwise override the envelope discriminator).
* Any malformed input raises :class:`ToolOkParseError`. The per-tool parser
  catches it and returns the existing ``ToolOutputContractViolation`` sentinel,
  so the executor's fail-closed path (``PlannerGapError`` →
  ``planner_gaps`` → unknown_outcome) is reused — NOT a new error channel.

All functions are pure (no I/O).
"""

from __future__ import annotations

import json
from typing import Any, Iterable

OKV2_PREFIX = "okv2:"


class ToolOkParseError(ValueError):
    """Raised when an ``okv2:`` wire string is malformed / fails envelope rules.

    The per-tool parser converts this to ``ToolOutputContractViolation`` so the
    executor records a controlled ``planner_gaps`` row instead of leaking a raw
    ``ValueError``/``JSONDecodeError`` into production.
    """


def _reject_json_constant(_token: str) -> float:  # pragma: no cover - raises always
    """``json.loads(parse_constant=...)`` hook — fail closed on NaN/Infinity."""
    raise ValueError("non-standard JSON constant (NaN/Infinity) not allowed")


def is_okv2(raw: str) -> bool:
    """True iff ``raw`` uses the new ``okv2:`` envelope (literal prefix match)."""
    return isinstance(raw, str) and raw.startswith(OKV2_PREFIX)


def encode_tool_ok(status: str, payload: dict[str, Any]) -> str:
    """Serialise a typed tool outcome to the ``okv2:<status>:<json>`` envelope.

    ``status`` is the output-model discriminator literal; ``payload`` carries the
    by-name fields. ``payload`` MUST NOT contain a reserved ``status`` key — the
    discriminator lives only in the envelope prefix.
    """
    if not isinstance(status, str) or not status:
        raise ToolOkParseError("encode_tool_ok: status must be a non-empty str")
    if ":" in status:
        # status is the second segment; a colon would break split(":", 2) parsing.
        raise ToolOkParseError(f"encode_tool_ok: status must not contain ':' ({status!r})")
    if not isinstance(payload, dict):
        raise ToolOkParseError("encode_tool_ok: payload must be a dict")
    if "status" in payload:
        raise ToolOkParseError(
            "encode_tool_ok: payload must not carry a reserved 'status' key "
            "(discriminator lives in the envelope prefix)"
        )
    # Codex Ф0 R1 [MAJOR]: NO ``default=str`` — a non-JSON payload value is a
    # producer-contract bug and must fail closed, not get silently stringified.
    # Codex Ф0 R2 [MAJOR]: ``allow_nan=False`` — reject NaN/Infinity so the wire
    # stays STRICT JSON (default json emits bare ``NaN``/``Infinity`` tokens).
    try:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ToolOkParseError(
            f"encode_tool_ok: payload is not strict-JSON-serialisable ({exc})"
        ) from exc
    return f"{OKV2_PREFIX}{status}:{body}"


def parse_tool_ok(raw: str, accepted_statuses: Iterable[str]) -> tuple[str, dict[str, Any]]:
    """Parse an ``okv2:`` wire string → ``(status, payload)``.

    Raises :class:`ToolOkParseError` on any of: wrong/absent prefix, fewer than
    three segments, empty payload, non-JSON / non-object payload, a payload that
    carries a reserved ``status`` key, or a ``status`` not in
    ``accepted_statuses``. The discriminator is taken ONLY from the envelope;
    the payload is never trusted for it.
    """
    # Codex Ф0 R1 [MINOR]: exception text MUST NOT echo raw tool output — these
    # errors flow to planner_gaps/contract-violation logs and raw content could
    # leak ids/names. Messages carry only shape metadata (lengths/counts/types).
    accepted = frozenset(accepted_statuses)
    if not is_okv2(raw):
        raise ToolOkParseError("parse_tool_ok: not an okv2 envelope (missing prefix)")
    # Three segments: "okv2", "<status>", "<json>". maxsplit=2 keeps colons in JSON.
    parts = raw.split(":", 2)
    if len(parts) != 3:
        raise ToolOkParseError(f"parse_tool_ok: expected 3 segments, got {len(parts)}")
    _prefix, status, body = parts
    if not status:
        raise ToolOkParseError("parse_tool_ok: empty status segment")
    if not body:
        raise ToolOkParseError("parse_tool_ok: empty payload segment")
    if status not in accepted:
        # Do not echo the received status (could be an id-like injected value).
        raise ToolOkParseError(
            f"parse_tool_ok: status not in accepted {sorted(accepted)!r}"
        )
    try:
        # Codex Ф0 R2 [MAJOR]: reject bare NaN/Infinity tokens — strict JSON only.
        payload = json.loads(body, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        # Drop the exception detail — it can contain slices of the raw payload.
        raise ToolOkParseError("parse_tool_ok: payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ToolOkParseError(
            f"parse_tool_ok: payload must be a JSON object, got {type(payload).__name__}"
        )
    if "status" in payload:
        raise ToolOkParseError(
            "parse_tool_ok: payload must not carry a reserved 'status' key"
        )
    return status, payload


__all__ = [
    "OKV2_PREFIX",
    "ToolOkParseError",
    "is_okv2",
    "encode_tool_ok",
    "parse_tool_ok",
]
