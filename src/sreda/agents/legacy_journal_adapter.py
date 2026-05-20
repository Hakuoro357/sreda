"""Adapter from the legacy LangChain tool loop to R39-style journal entries.

This module observes already executed legacy tool calls. It never invokes tools
itself and deliberately stores only small sanitized metadata.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any

from sreda.agents.contracts import ResultKind, ToolJournalEntry
from sreda.agents.journal import ToolJournal
from sreda.services.write_intent_validator import HOUSEWIFE_MUTATING_TOOL_NAMES


UI_ONLY_TOOL_NAMES = frozenset({"reply_with_buttons"})


@dataclass(frozen=True)
class LegacyToolDispatchRecord:
    tool_call_id: str
    tool_name: str
    args: dict[str, Any]
    result_str: str
    is_physical: bool
    loop_index: int
    batch_index: int
    tenant_id: str
    user_id: str | None
    run_id: str
    feature_key: str | None


@dataclass(frozen=True)
class LegacyJournalAppendOutcome:
    added: bool
    duplicate_skipped: bool = False


@dataclass(frozen=True)
class LegacyJournalEnforcementResult:
    text: str
    reply_markup: dict | None
    audit_unbacked: bool
    replaced: bool = False


_ENTITY_ID_RE = re.compile(
    r"\b(?:rem|task|recipe|menu|mpi|sh|cl|clitem|fam)_[A-Za-z0-9_]+\b"
)


def append_legacy_dispatch(
    journal: ToolJournal,
    record: LegacyToolDispatchRecord,
) -> LegacyJournalAppendOutcome:
    """Append one executed legacy tool call to ``journal``.

    ``is_physical=False`` is the legacy duplicate-replay signal produced by
    ``_dispatch_tool_calls_batch``. Such calls are intentionally not journaled
    as side effects.
    """

    if not record.is_physical:
        return LegacyJournalAppendOutcome(added=False, duplicate_skipped=True)
    entry = build_legacy_journal_entry(record)
    return LegacyJournalAppendOutcome(added=journal.append(entry))


def build_legacy_journal_entry(record: LegacyToolDispatchRecord) -> ToolJournalEntry:
    tool_name = record.tool_name or ""
    known_mutating = tool_name in HOUSEWIFE_MUTATING_TOOL_NAMES
    ui_only = tool_name in UI_ONLY_TOOL_NAMES
    result_kind = _classify_result(record.result_str)
    safe_summary = _safe_summary(record.result_str)
    result_data: dict[str, Any] = {
        "summary": safe_summary,
        "known_mutating_tool": known_mutating,
        "ui_only": ui_only,
        "physical": bool(record.is_physical),
        "feature_key": record.feature_key,
    }
    return ToolJournalEntry(
        tool_name=tool_name,
        action_index=(record.loop_index * 1000) + record.batch_index,
        result_kind=result_kind,
        result_data=result_data,
        error_message=safe_summary if result_kind is ResultKind.FAILURE else None,
        entity_id=_extract_entity_id(record.result_str),
        idempotency_key=(
            f"legacy:{record.tenant_id}:{record.run_id}:{record.tool_call_id}"
        ),
        error_code=_extract_error_code(record.result_str)
        if result_kind is ResultKind.FAILURE else None,
    )


def successful_tool_names_for_audit(journal: ToolJournal) -> set[str]:
    return {
        e.tool_name for e in journal
        if e.result_kind is ResultKind.SUCCESS
        and _is_auditable_physical_mutation(e)
    }


def attempted_tool_names_for_audit(journal: ToolJournal) -> set[str]:
    return {
        e.tool_name for e in journal
        if _is_auditable_physical_mutation(e)
    }


def side_effects_count(journal: ToolJournal) -> int:
    return sum(
        1 for e in journal
        if e.result_kind is ResultKind.SUCCESS and _is_auditable_physical_mutation(e)
    )


def enforce_legacy_journal_response(
    *,
    text: str,
    reply_markup: dict | None,
    journal: ToolJournal,
    enforce_enabled: bool,
    journal_unreliable: bool,
    detector: Callable[[str, set[str]], bool],
    safe_ack_fn: Callable[[set[str]], str],
) -> LegacyJournalEnforcementResult:
    """Audit final user-facing text/markup against the legacy journal.

    When replacement happens, stale button markup is cleared by default so a
    neutral/safe text cannot be paired with old LLM-generated choices.
    """

    successful_tools = successful_tool_names_for_audit(journal)
    if journal_unreliable and enforce_enabled:
        return LegacyJournalEnforcementResult(
            text="Не смогла подтвердить действие. Попробуй ещё раз.",
            reply_markup=None,
            audit_unbacked=True,
            replaced=True,
        )
    try:
        audit_unbacked = bool(detector(text or "", successful_tools))
    except Exception:
        audit_unbacked = False
    if not enforce_enabled or not audit_unbacked:
        return LegacyJournalEnforcementResult(
            text=text,
            reply_markup=reply_markup,
            audit_unbacked=audit_unbacked,
            replaced=False,
        )
    replacement = (
        safe_ack_fn(successful_tools)
        if successful_tools
        else "Не смогла подтвердить действие. Попробуй ещё раз."
    )
    return LegacyJournalEnforcementResult(
        text=replacement,
        reply_markup=None,
        audit_unbacked=True,
        replaced=True,
    )


def _is_auditable_physical_mutation(entry: ToolJournalEntry) -> bool:
    return (
        bool(entry.tool_name)
        and entry.result_data.get("known_mutating_tool") is True
        and entry.result_data.get("ui_only") is not True
        and entry.result_data.get("physical") is True
    )


def _classify_result(result_str: str) -> ResultKind:
    text = (result_str or "").strip().lower()
    if text.startswith("ok:"):
        return ResultKind.SUCCESS
    if text.startswith("partial:"):
        return ResultKind.PARTIAL
    return ResultKind.FAILURE


def _extract_entity_id(result_str: str) -> str | None:
    match = _ENTITY_ID_RE.search(result_str or "")
    return match.group(0) if match else None


def _extract_error_code(result_str: str) -> str | None:
    text = (result_str or "").strip()
    if not text.startswith("error:"):
        return None
    parts = text.split(":", 2)
    if len(parts) >= 2 and parts[1]:
        return parts[1][:64]
    return "error"


def _safe_summary(result_str: str) -> str:
    text = (result_str or "").strip()
    if not text:
        return "empty"
    prefix = text.split(":", 1)[0].lower()
    if prefix in {"ok", "error", "partial"}:
        return prefix
    return "unknown"
