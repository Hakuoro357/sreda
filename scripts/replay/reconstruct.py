"""Offline-replay reconstruction for harness Phase B (#87).

This module:
  1. Defines ``ReplayExportRecord`` — the frozen contract that
     ``probe_replay_export.py`` (VDS-side exporter) must produce.
  2. Provides ``build_prompt_args`` to map one export record to a
     ``PromptBuildArgs`` (historical ``now``, NOT datetime.now()).
  3. Provides the §4.3 fidelity gate: ``reconstruction_fidelity`` and
     ``is_fidelity_ok``.
  4. Provides manifest loading and gold-case pairing: ``load_manifest``
     and ``pair_with_gold``.

PURE / LOCAL ONLY — no prod DB, no network, no LLM, no SSH, no real writes.
All prod identifiers are replaced by opaque placeholders in exported data.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Import sreda prompt-builder types (pure dataclass — no side effects).
# We do sys.path manipulation so the module can be imported from both the
# scripts/ context and from tests (which add the same src/ dir).
# ---------------------------------------------------------------------------

_SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from sreda.runtime.planner.prompt_builder import (  # noqa: E402
    MemorySnapshot,
    NowMoment,
    ProfileSnapshot,
    PromptBuildArgs,
    TurnMessage,
    TurnSnapshot,
    VoiceMeta,
)

# Re-export so callers that import from reconstruct get everything they need.
__all__ = [
    "MemorySnapshot",
    "NowMoment",
    "PairingResult",
    "ProfileSnapshot",
    "PromptBuildArgs",
    "ReconstructedCase",
    "ReplayExportRecord",
    "TurnMessage",
    "TurnSnapshot",
    "VoiceMeta",
    "build_prompt_args",
    "is_fidelity_ok",
    "load_export_records",
    "load_manifest",
    "pair_with_gold",
    "reconstruction_fidelity",
]

# The manifest module lives next to this file.
_MANIFEST_DIR = Path(__file__).resolve().parent
if str(_MANIFEST_DIR) not in sys.path:
    sys.path.insert(0, str(_MANIFEST_DIR))

from manifest import ReplayCase  # noqa: E402

# ---------------------------------------------------------------------------
# Field-status vocabulary
# ---------------------------------------------------------------------------

_STATUS_OK = "ok"
_STATUS_MISSING = "missing"
_STATUS_DEGRADED = "degraded"

# Critical fields: ALL must be "ok" for a record to pass the gate (§4.3).
_CRITICAL_FIELDS: tuple[str, ...] = (
    "user_text",
    "now_tz_locale",
    "baseline_alignment",
    "decrypted_ok",
)


# ---------------------------------------------------------------------------
# Export-record CONTRACT
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _VoiceMetaDict:
    """Typed voice sub-record (intermediate; JSON arrives as plain dict)."""

    is_voice: bool
    confidence: float


@dataclass(frozen=True)
class ReplayExportRecord:
    """One exported turn from ``probe_replay_export.py``.

    This dataclass IS the contract between the VDS-side exporter and the
    local harness.  The exporter must produce JSONL where each line
    deserialises to this shape.

    Fields
    ------
    turn_id_hash :
        Opaque SHA-256 hex prefix (16 chars) of the real turn id.
        Used for correlation and manifest lookup without PII exposure.
    tenant_placeholder :
        Redacted tenant id safe for the repo (e.g. ``"tenant_tg_<SMOKE>"``).
        The real Telegram chat_id lives only in the local quarantine export.

    user_message :
        Plaintext user text from ``agent_runs.input_json.source_value``.

    now_iso :
        Historical ``turn.created_at`` in ISO-8601 with timezone offset,
        e.g. ``"2026-05-10T14:23:00+03:00"``.
    timezone_name :
        IANA timezone name stored alongside the turn (e.g. ``"Europe/Moscow"``).
        Required for ``NowMoment`` — not the UTC offset, the symbolic name.
    locale :
        BCP-47 locale string (e.g. ``"ru"`` or ``"ru-RU"``).

    voice :
        ``{"is_voice": bool, "confidence": float}`` or ``None`` when the
        message was text-only.

    profile :
        ``{"name": str, "gender": str, "address": str, "timezone": str,
           "language": str}`` — current-state profile snapshot (Q2).
        May be partial (missing keys → empty string / default).

    memories :
        List of ``{"content": str, "source": str, "score": float,
        "saved_at": str}`` — re-recalled with current memory at export time.

    history :
        ``{"active_turn": TurnSnapshot-shaped | null,
           "closed_turns": [TurnSnapshot-shaped]}``
        where each TurnSnapshot-shaped dict has
        ``{turn_id, started_at, summary, messages: [{role, text, ts}],
           is_active}``.

    baseline :
        ``{"old_reply": str,
           "old_called_tools": [str],
           "old_side_effects": [...]}``
        Old bot reply (decrypted ``v2:`` payload) and archived tool calls.
        May be partial — missing fields drive ``unknown`` scoring.

    reconstruction_status :
        Per-field probe of reconstruction quality produced by the exporter.
        Keys: ``user_text``, ``now_tz_locale``, ``profile``,
        ``memory_provenance``, ``history_window``, ``active_turn``,
        ``voice_meta``, ``baseline_alignment``, ``decrypted_ok``.
        Values: ``"ok"`` | ``"missing"`` | ``"degraded"``.
    """

    turn_id_hash: str
    tenant_placeholder: str

    # --- user input ---
    user_message: str
    now_iso: str
    timezone_name: str
    locale: str

    # --- optional modality ---
    voice: dict[str, Any] | None  # {"is_voice": bool, "confidence": float}

    # --- planner context (current-state, Q2) ---
    profile: dict[str, Any]       # {name, gender, address, timezone, language}
    memories: list[dict[str, Any]]  # [{content, source, score, saved_at}]
    history: dict[str, Any]       # {active_turn, closed_turns}

    # --- baseline (old behaviour) ---
    baseline: dict[str, Any]      # {old_reply, old_called_tools, old_side_effects}

    # --- per-field reconstruction quality (set by exporter) ---
    reconstruction_status: dict[str, str]

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain JSON-compatible dict."""
        return {
            "turn_id_hash": self.turn_id_hash,
            "tenant_placeholder": self.tenant_placeholder,
            "user_message": self.user_message,
            "now_iso": self.now_iso,
            "timezone_name": self.timezone_name,
            "locale": self.locale,
            "voice": self.voice,
            "profile": self.profile,
            "memories": self.memories,
            "history": self.history,
            "baseline": self.baseline,
            "reconstruction_status": self.reconstruction_status,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ReplayExportRecord":
        """Deserialise from a plain dict (one JSONL line).

        ALL contract fields are REQUIRED except ``voice`` (present only for
        voice turns). A missing required key raises ``KeyError`` — a malformed
        export must fail LOUDLY, not silently default (Codex review): e.g. a
        missing ``baseline`` defaulting to ``{}`` could pass while the status
        claims ``baseline_alignment="ok"``.
        """
        required = (
            "turn_id_hash", "tenant_placeholder", "user_message", "now_iso",
            "timezone_name", "locale", "profile", "memories", "history",
            "baseline", "reconstruction_status",
        )
        missing = [k for k in required if k not in d]
        if missing:
            raise KeyError(
                f"ReplayExportRecord missing required contract field(s): {missing}"
            )
        return cls(
            turn_id_hash=d["turn_id_hash"],
            tenant_placeholder=d["tenant_placeholder"],
            user_message=d["user_message"],
            now_iso=d["now_iso"],
            timezone_name=d["timezone_name"],
            locale=d["locale"],
            voice=d.get("voice"),  # optional — only for voice turns
            profile=d["profile"],
            memories=d["memories"],
            history=d["history"],
            baseline=d["baseline"],
            reconstruction_status=d["reconstruction_status"],
        )


def load_export_records(path: str | Path) -> list[ReplayExportRecord]:
    """Load a JSONL export file into a list of ``ReplayExportRecord``.

    Skips blank lines and comment lines (starting with ``#``).
    Raises ``json.JSONDecodeError`` or ``KeyError`` on malformed records.
    """
    records: list[ReplayExportRecord] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                raise json.JSONDecodeError(
                    f"line {lineno}: {exc.msg}", exc.doc, exc.pos
                ) from exc
            records.append(ReplayExportRecord.from_dict(data))
    return records


# ---------------------------------------------------------------------------
# Build PromptBuildArgs from an export record
# ---------------------------------------------------------------------------


def _build_profile(
    profile_dict: dict[str, Any],
    *,
    default_timezone: str,
    default_language: str,
) -> ProfileSnapshot:
    """Map profile dict → ProfileSnapshot.

    When the profile dict lacks ``timezone`` / ``language``, fall back to the
    RECORD-level ``timezone_name`` / ``locale`` (authoritative for the turn) —
    NOT the hardcoded ru/Europe-Moscow defaults — so the now_tz_locale contract
    is preserved for non-ru / non-MSK turns (Codex review).
    """
    return ProfileSnapshot(
        name=profile_dict.get("name", ""),
        gender=profile_dict.get("gender", ""),
        address=profile_dict.get("address", ""),
        timezone=profile_dict.get("timezone") or default_timezone,
        language=profile_dict.get("language") or default_language,
    )


def _build_memories(memories_list: list[dict[str, Any]]) -> tuple[MemorySnapshot, ...]:
    """Map list of memory dicts → tuple of MemorySnapshot."""
    result: list[MemorySnapshot] = []
    for m in memories_list:
        result.append(
            MemorySnapshot(
                content=m.get("content", ""),
                source=m.get("source", "memory:core"),
                score=float(m.get("score", 0.0)),
                saved_at=m.get("saved_at", ""),
            )
        )
    return tuple(result)


def _build_turn_messages(messages_list: list[dict[str, Any]]) -> list[TurnMessage]:
    """Map list of message dicts → list of TurnMessage."""
    result: list[TurnMessage] = []
    for msg in messages_list:
        result.append(
            TurnMessage(
                role=msg.get("role", "юзер"),
                text=msg.get("text", ""),
                ts=msg.get("ts", ""),
            )
        )
    return result


def _build_turn_snapshot(turn_dict: dict[str, Any] | None) -> TurnSnapshot | None:
    """Map a TurnSnapshot-shaped dict → TurnSnapshot, or None."""
    if turn_dict is None:
        return None
    return TurnSnapshot(
        turn_id=turn_dict.get("turn_id", ""),
        started_at=turn_dict.get("started_at", ""),
        summary=turn_dict.get("summary"),
        messages=_build_turn_messages(turn_dict.get("messages", [])),
        is_active=bool(turn_dict.get("is_active", False)),
    )


def build_prompt_args(
    record: ReplayExportRecord,
    *,
    tool_specs: tuple,
    composer_template_ids: tuple[str, ...],
    composer_llm_prompt_keys: tuple[str, ...],
    few_shot_block: str,
) -> PromptBuildArgs:
    """Map a ``ReplayExportRecord`` to ``PromptBuildArgs``.

    Historical ``now`` is taken from ``record.now_iso`` — NOT ``datetime.now()``.
    This is critical for replay fidelity (p-002 / §4.1).

    Tool registry inputs (``tool_specs``, ``composer_template_ids``,
    ``composer_llm_prompt_keys``, ``few_shot_block``) are the CURRENT
    production registry — the system under test — NOT historical snapshots.

    Parameters
    ----------
    record :
        The exported turn.
    tool_specs :
        Current tool registry entries (``tuple[ToolSpec, ...]``).
    composer_template_ids :
        Current composer template id tuple.
    composer_llm_prompt_keys :
        Current composer LLM prompt key tuple.
    few_shot_block :
        Current few-shot block string.
    """
    # Historical now — MUST NOT use datetime.now()
    now_dt = datetime.fromisoformat(record.now_iso)
    # Fail-closed on a naive timestamp: the contract is ISO WITH offset; a naive
    # now would silently misrepresent the historical moment (Codex review).
    if now_dt.tzinfo is None:
        raise ValueError(
            f"now_iso must be timezone-aware (ISO-8601 with offset); got naive "
            f"{record.now_iso!r}. Reconstruction must not proceed on a naive now."
        )
    now_moment = NowMoment(now=now_dt, timezone_name=record.timezone_name)

    profile = _build_profile(
        record.profile,
        default_timezone=record.timezone_name,
        default_language=record.locale,
    )
    memories = _build_memories(record.memories)

    history_dict = record.history
    active_turn = _build_turn_snapshot(history_dict.get("active_turn"))
    closed_turns = tuple(
        ts
        for ts_dict in history_dict.get("closed_turns", [])
        if (ts := _build_turn_snapshot(ts_dict)) is not None
    )

    # Voice meta — present only when the message was voice
    voice_meta: VoiceMeta | None = None
    if record.voice is not None:
        voice_meta = VoiceMeta(
            is_voice=bool(record.voice.get("is_voice", False)),
            confidence=float(record.voice.get("confidence", 1.0)),
        )

    return PromptBuildArgs(
        tool_specs=tuple(tool_specs),
        composer_template_ids=composer_template_ids,
        composer_llm_prompt_keys=composer_llm_prompt_keys,
        few_shot_block=few_shot_block,
        profile=profile,
        memories=memories,
        active_turn=active_turn,
        closed_turns=closed_turns,
        now=now_moment,
        user_message=record.user_message,
        voice_meta=voice_meta,
    )


# ---------------------------------------------------------------------------
# Reconstruction fidelity gate (§4.3)
# ---------------------------------------------------------------------------

# Canonical set of per-field status keys (§4.3).
_FIDELITY_KEYS: tuple[str, ...] = (
    "user_text",
    "now_tz_locale",
    "profile",
    "memory_provenance",
    "history_window",
    "active_turn",
    "voice_meta",
    "baseline_alignment",
    "decrypted_ok",
)


def _tz_name_valid(tz_name: str) -> bool:
    """Best-effort IANA-timezone validity check via ``zoneinfo`` (Codex R2).

    Tolerant of environments without the tz database: if ``zoneinfo`` is absent
    or the tz DB is unavailable (even ``UTC`` fails to resolve), we cannot
    validate names, so do NOT false-degrade — return True. When the DB IS
    available, an unknown ``tz_name`` returns False (→ now_tz_locale degraded).
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return True
    try:
        ZoneInfo("UTC")  # tz DB present?
    except Exception:  # noqa: BLE001 — tz DB unavailable → cannot validate
        return True
    try:
        ZoneInfo(tz_name)
        return True
    except Exception:  # noqa: BLE001 — unknown/invalid zone
        return False


# Minimal BCP-47-ish locale syntax: a 2-3 letter primary language subtag,
# optional 2-8 alnum subtags (region/script). Accepts ru, ru-RU, en, en-US;
# rejects "not-a-locale", "??", "" (Codex R3 — non-blank alone is insufficient).
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


def _locale_valid(locale: str) -> bool:
    """True if ``locale`` looks like a valid BCP-47 tag (syntax-level)."""
    return bool(_LOCALE_RE.match((locale or "").strip()))


def reconstruction_fidelity(record: ReplayExportRecord) -> dict[str, str]:
    """Return per-field reconstruction status for ``record``.

    The status dict comes from the exporter (``record.reconstruction_status``).
    This function normalises it: any key absent from the exporter output is
    treated as ``"missing"``.

    Returns a dict with exactly the keys in ``_FIDELITY_KEYS``.
    """
    raw = record.reconstruction_status
    status = {key: raw.get(key, _STATUS_MISSING) for key in _FIDELITY_KEYS}
    # Consumer-side validation (Codex review R1+R2): do NOT blindly trust the
    # exporter's now_tz_locale="ok". The CRITICAL now_tz_locale field is only
    # genuinely "ok" when ALL of: now_iso parses + is tz-aware, timezone_name is
    # non-blank AND a valid IANA zone, and locale is non-blank. Otherwise downgrade
    # so the CRITICAL gate excludes the record regardless of the claimed status.
    try:
        now_aware = datetime.fromisoformat(record.now_iso).tzinfo is not None
    except (ValueError, TypeError):
        status["now_tz_locale"] = _STATUS_MISSING
        return status
    tz_ok = bool((record.timezone_name or "").strip()) and _tz_name_valid(record.timezone_name)
    locale_ok = _locale_valid(record.locale)
    if not (now_aware and tz_ok and locale_ok):
        status["now_tz_locale"] = _STATUS_DEGRADED
    return status


def is_fidelity_ok(
    record: ReplayExportRecord,
    *,
    critical_fields: tuple[str, ...] = _CRITICAL_FIELDS,
) -> bool:
    """Return True when all ``critical_fields`` have status ``"ok"``.

    Records that fail this gate are EXCLUDED from the headline verdict
    (§4.3 / §12 Stage-0).  Count them separately; do NOT score them.
    """
    status = reconstruction_fidelity(record)
    return all(status.get(f) == _STATUS_OK for f in critical_fields)


# ---------------------------------------------------------------------------
# Manifest loading and gold-case pairing
# ---------------------------------------------------------------------------


def load_manifest(path: str | Path) -> dict[str, ReplayCase]:
    """Load ``replay_cases.jsonl`` into a ``{turn_id_hash: ReplayCase}`` map.

    The manifest JSONL format is one JSON object per line encoding a
    ``ReplayCase``.  Fields:

    .. code-block:: json

        {
          "turn_id_hash": "abcdef0123456789",
          "tenant_placeholder": "tenant_tg_<SMOKE>",
          "expectations": {
            "expected_intent": "read",
            "memory_dependence": "independent",
            "current_memory_status": "not_applicable",
            ...
          }
        }

    ``reconstruction_status`` is NOT stored in the manifest (it is
    populated by Phase B reconstruction); any value in the file is
    ignored and set to ``None``.
    """
    from manifest import ReplayCase, ReplayExpectations  # local import — avoids circular

    cases: dict[str, ReplayCase] = {}
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                raise json.JSONDecodeError(
                    f"manifest line {lineno}: {exc.msg}", exc.doc, exc.pos
                ) from exc

            exp_dict = data.get("expectations", {})
            expectations = ReplayExpectations(
                expected_intent=exp_dict["expected_intent"],
                required_outcomes=tuple(exp_dict.get("required_outcomes", [])),
                acceptable_action_sets=tuple(
                    tuple(a) for a in exp_dict.get("acceptable_action_sets", [])
                ),
                allowed_write_targets=tuple(exp_dict.get("allowed_write_targets", [])),
                forbidden_write_targets=tuple(
                    exp_dict.get("forbidden_write_targets", [])
                ),
                forbidden_actions=tuple(exp_dict.get("forbidden_actions", [])),
                requires_clarification=bool(
                    exp_dict.get("requires_clarification", False)
                ),
                exactness_rule=exp_dict.get("exactness_rule", ""),
                memory_dependence=exp_dict.get("memory_dependence", "independent"),
                current_memory_status=exp_dict.get(
                    "current_memory_status", "not_applicable"
                ),
                eligibility_reason=exp_dict.get("eligibility_reason", ""),
                must_cover=bool(exp_dict.get("must_cover", False)),
                incident_id=exp_dict.get("incident_id", ""),
                label_source=exp_dict.get("label_source", "manual"),
                labeler=exp_dict.get("labeler", ""),
                confidence=float(exp_dict.get("confidence", 1.0)),
            )
            case = ReplayCase(
                turn_id_hash=data["turn_id_hash"],
                tenant_placeholder=data["tenant_placeholder"],
                expectations=expectations,
                reconstruction_status=None,  # populated by Phase B, not manifest
            )
            cases[case.turn_id_hash] = case
    return cases


@dataclass
class ReconstructedCase:
    """A paired export record + gold ``ReplayCase`` + fidelity status.

    ``prompt_args`` is built on demand (lazy) via ``build_prompt_args``
    — callers supply registry arguments at build time.

    ``gold`` may be ``None`` when no matching manifest entry was found;
    callers should flag and handle these (relevant for must_cover cases).
    """

    record: ReplayExportRecord
    gold: ReplayCase | None
    fidelity: dict[str, str]
    unmatched: bool = field(init=False)

    def __post_init__(self) -> None:
        self.unmatched = self.gold is None

    @property
    def fidelity_ok(self) -> bool:
        """True when all critical fidelity fields are ``"ok"``."""
        return is_fidelity_ok(self.record)


@dataclass
class PairingResult:
    """Outcome of pairing export records with the frozen gold manifest.

    Surfaces BOTH directions of mismatch (Codex review):
    - ``cases`` — one ReconstructedCase per export record (matched or not).
    - ``unmatched_gold`` — gold cases present in the manifest but ABSENT from
      the export. A ``must_cover=True`` gold case here would be invisible to a
      per-record loop, so the runner could miss the Stage-0 NO DECISION blocker.
    - ``missing_must_cover`` — the subset of ``unmatched_gold`` with
      ``must_cover=True``; non-empty ⟹ the runner MUST return Stage-0 NO DECISION.
    """

    cases: list[ReconstructedCase]
    unmatched_gold: list[ReplayCase]
    missing_must_cover: list[ReplayCase]


def pair_with_gold(
    records: list[ReplayExportRecord],
    manifest: dict[str, ReplayCase],
) -> PairingResult:
    """Pair export records with their gold manifest entries (by ``turn_id_hash``).

    Records without a matching manifest entry get ``gold=None`` /
    ``unmatched=True``.  Gold cases absent from the export are reported in
    ``unmatched_gold`` (and ``missing_must_cover`` for the ``must_cover=True``
    subset) so the runner can enforce the Stage-0 NO DECISION blocker.
    """
    cases: list[ReconstructedCase] = []
    seen_hashes: set[str] = set()
    for record in records:
        gold = manifest.get(record.turn_id_hash)
        fidelity = reconstruction_fidelity(record)
        cases.append(ReconstructedCase(record=record, gold=gold, fidelity=fidelity))
        seen_hashes.add(record.turn_id_hash)

    unmatched_gold = [
        gold for h, gold in manifest.items() if h not in seen_hashes
    ]
    missing_must_cover = [
        gold for gold in unmatched_gold if gold.expectations.must_cover
    ]
    return PairingResult(
        cases=cases,
        unmatched_gold=unmatched_gold,
        missing_must_cover=missing_must_cover,
    )
