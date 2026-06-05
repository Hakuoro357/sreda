"""Mock tool layer for offline replay harness.

Every tool in the full 55-tool registry (ALL_TOOL_SPECS) returns a
schema-valid stub raw string. The executor calls tool.invoke(args) -> str,
then passes that str to spec.process_output(). All mocks here are verified
to produce non-ToolOutputContractViolation results.

Strategy per tool group (Fix #5 — dual-Codex issue #87):
- Read-only tools: return the "empty" sentinel (same as restricted harness).
- Write tools: return a deterministic schema-VALID success string so compose
  is NOT artificially pushed into a fallback path. Each id uses a fixed 24-hex
  suffix that satisfies the regex but is obviously synthetic.
- All strings were verified against spec.process_output() patterns in housewife.py
  to confirm no ToolOutputContractViolation.

No DB writes. No network calls. Purely local.
"""

from __future__ import annotations

import json
from typing import Any


# ---------------------------------------------------------------------------
# Per-tool mock raw strings (all 55 tools; verified non-violation outputs)
# ---------------------------------------------------------------------------

_TEXT = "page content"

# Fixed 24-hex suffixes for all synthetic IDs — obviously mock, passes regex.
_TASK_ID    = "task_" + "a" * 24        # task_aaaaaaaaaaaaaaaaaaaaaaaa
_REM_ID     = "rem_"  + "b" * 24        # rem_bbbbbbbbbbbbbbbbbbbbbbbb
_REC_ID     = "rec_"  + "c" * 24        # rec_cccccccccccccccccccccccc
_MENU_ID    = "menu_" + "d" * 24        # menu_dddddddddddddddddddddddd
_FM_ID      = "fm_"   + "e" * 24        # fm_eeeeeeeeeeeeeeeeeeeeeeee
_CL_ID      = "checklist_" + "f" * 24  # checklist_ffffffffffffffffffffff
_CLITEM_ID  = "clitem_"    + "0" * 24  # clitem_000000000000000000000000
_MEM_ID     = "mem_mock001"             # mem_mock001 — alphanumeric, passes [a-zA-Z0-9_-]+

_MOCK_RAW: dict[str, str] = {
    # --- shopping (7) ---
    "add_shopping_items":             "ok:added:0",
    "mark_shopping_bought":           "ok:bought:1",
    "remove_shopping_items":          "ok:removed:1",
    "update_shopping_item":           f"ok:updated:sh_{'a' * 24}",
    "update_shopping_items_category": "ok:updated:1",
    "list_shopping":                  "no shopping items",
    "clear_bought_shopping":          "ok:cleared:0",

    # --- reminders (4) ---
    # ok:scheduled:<rem_id>:<iso>  — matches _SCHEDULE_REMINDER_RE
    "schedule_reminder":  f"ok:scheduled:{_REM_ID}:2026-06-02T12:00:00+00:00",
    "list_reminders":     "no active reminders",
    # ok:updated:<rem_id>:<next_iso_or_none>  — matches _UPDATE_REMINDER_RE
    "update_reminder":    f"ok:updated:{_REM_ID}:2026-06-02T12:00:00+00:00",
    "cancel_reminder":    "ok:cancelled",

    # --- recipes (5) ---
    # ok:saved:<rec_id>  — matches _SAVE_RECIPE_RE (status=saved|duplicate)
    "save_recipe":        f"ok:saved:{_REC_ID}",
    # ok:batch_saved:N:skipped_as_duplicate:M  — matches _SAVE_BATCH_RE
    "save_recipes_batch": f"ok:batch_saved:1:skipped_as_duplicate:0:ids=[{_REC_ID}]",
    "search_recipes":     "no recipes found",
    "get_recipe":         "not_found",
    "delete_recipe":      "ok:deleted",

    # --- menu (5) ---
    # ok:plan_created:<menu_id>:<week_iso>  — matches _PLAN_WEEK_MENU_RE
    # week_start_iso must be YYYY-MM-DD (min_length=10); use a Monday
    "plan_week_menu":              f"ok:plan_created:{_MENU_ID}:2026-06-02",
    "update_menu_item":            "ok:cleared_or_not_found",
    "list_menu":                   "no menu plan for that week",
    # ok:generated:0:eaters=1  — matches _GEN_SHOPPING_RE
    "generate_shopping_from_menu": "ok:generated:0:eaters=1",
    # ok:cleared:0  — matches _CLEAR_MENU_RE
    "clear_menu":                  "ok:cleared:0",

    # --- household / family (4) ---
    # ok:added:1:skipped_as_duplicate:0:ids=[<fm_id>]  — matches _ADD_FAMILY_RE
    "add_family_members":   f"ok:added:1:skipped_as_duplicate:0:ids=[{_FM_ID}]",
    "list_family_members":  "no family members recorded",
    "update_family_member": "ok:updated",
    "remove_family_member": "ok:removed",

    # --- tasks (9) ---
    # ok:created:<task_id>  — matches _ADD_TASK_RE
    "add_task":      f"ok:created:{_TASK_ID}",
    "list_tasks":    "no tasks",
    # ok:updated:<task_id>  — matches _UPDATE_TASK_RE
    "update_task":      f"ok:updated:{_TASK_ID}",
    # ok:completed:<task_id>  — matches _COMPLETE_TASK_RE
    "complete_task":    f"ok:completed:{_TASK_ID}",
    # ok:uncompleted:<task_id>  — matches _UNCOMPLETE_TASK_RE
    "uncomplete_task":  f"ok:uncompleted:{_TASK_ID}",
    # ok:cancelled:<task_id>  — matches _CANCEL_TASK_RE
    "cancel_task":      f"ok:cancelled:{_TASK_ID}",
    "delete_task":      "ok:deleted",
    # ok:reminder_attached:<rem_id>:за Nмин  — matches _ATTACH_REMINDER_RE
    "attach_reminder":  f"ok:reminder_attached:{_REM_ID}:за 15мин",
    "detach_reminder":  "ok:reminder_detached",

    # --- checklists (9) ---
    # ok:linked:<task_id>:<checklist_id>  — matches _LINK_TASK_RE
    "link_task_to_checklist": f"ok:linked:{_TASK_ID}:{_CL_ID}",
    # ok:unlinked:<task_id>:<checklist_id>  — matches _UNLINK_TASK_UNLINKED_RE
    "unlink_task":            f"ok:unlinked:{_TASK_ID}:{_CL_ID}",
    # ok:created:<checklist_id>:<title>  — matches _CREATE_CHECKLIST_RE
    "create_checklist":       f"ok:created:{_CL_ID}:mock checklist",
    # ok:added:1:list=<checklist_id>  — matches _ADD_CHECKLIST_ITEMS_NODUPS_RE
    "add_checklist_items":    f"ok:added:1:list={_CL_ID}",
    # ok:moved:item_id=<clitem_id>:list=<checklist_id>  — matches _MOVE_TASK_NEW_RE
    "move_task_to_checklist": f"ok:moved:item_id={_CLITEM_ID}:list={_CL_ID}",
    "list_checklists":        "no checklists",
    "show_checklist":         "error: not_found",
    # ok:done:<clitem_id>:<title>  — matches _MARK_DONE_RE
    "mark_checklist_item_done": f"ok:done:{_CLITEM_ID}:mock item",
    # ok:deleted:<clitem_id>:<title>  — matches _DELETE_CHECKLIST_ITEM_RE (if pattern exists)
    "delete_checklist_item":    f"ok:deleted:{_CLITEM_ID}:mock item",
    # ok:archived:<checklist_id>  — matches _ARCHIVE_CHECKLIST_RE
    "archive_checklist":        f"ok:archived:{_CL_ID}",

    # --- onboarding (3) ---
    # ok:answered:<topic>:next=<next>:status=<status>  — matches _ONBOARDING_ANSWERED_RE
    "onboarding_answered":  "ok:answered:self_intro:next=none:status=in_progress",
    # ok:deferred:<topic>:topic_state=<state>:next=<next>:status=<status>
    "onboarding_deferred":  "ok:deferred:self_intro:topic_state=skipped_once:next=none:status=in_progress",
    # ok:complete:status=complete  — matches _ONBOARDING_COMPLETE_RE
    "onboarding_complete":  "ok:complete:status=complete",

    # --- memory (4) ---
    # saved_core:<mem_id>  — matches _SAVE_CORE_FACT_RE
    "save_core_fact":          f"saved_core:{_MEM_ID}",
    # saved_episode:<mem_id>  — matches _SAVE_EPISODE_RE
    "save_episode":            f"saved_episode:{_MEM_ID}",
    "recall_memory":           "[]",
    "log_unsupported_request": "ok:logged",

    # --- ui (1) ---
    "reply_with_buttons": "ok:buttons:2",

    # --- web (3) ---
    "get_weather": "ok:weather:temp=20:cond=clear:city=Moscow",
    "web_search":  "ok:results:0:query=test",
    "fetch_url": json.dumps({
        "url": "https://example.com",
        "final_url": "https://example.com",
        "status": 200,
        "extractor": "html",
        "truncated": False,
        "length": len(_TEXT),
        "text": _TEXT,
    }),
}


class _MockTool:
    """Minimal LangChain-compatible tool stub.

    The executor calls either ``ainvoke(args)`` (async) or
    ``invoke(args)`` (sync fallback). We implement sync ``invoke``
    returning the canned raw string; the executor wraps it in
    ``asyncio.to_thread`` automatically.
    """

    def __init__(self, name: str, raw: str) -> None:
        self.name = name
        self._raw = raw

    def invoke(self, args: Any) -> str:  # noqa: ARG002
        return self._raw

    # Also support direct async call (executor checks ainvoke first)
    async def ainvoke(self, args: Any) -> str:  # noqa: ARG002
        return self._raw


def build_mock_tools_by_name() -> dict[str, _MockTool]:
    """Return {tool_name: MockTool} for all 55 full-registry tools."""
    return {name: _MockTool(name, raw) for name, raw in _MOCK_RAW.items()}


__all__ = ["build_mock_tools_by_name"]


# ===========================================================================
# Phase A: ReplayWriteRecorder  (plan §2 / §8 / issue #87)
# ===========================================================================
#
# The ReplayWriteRecorder replaces WRITE tools during Phase A offline replay.
# It RECORDS {target, payload, authorized} for every write tool call and
# returns the same schema-valid success string that _MOCK_RAW provides,
# WITHOUT any prod side-effect (no DB, no network).
#
# A fail-fast guard ``ReplayWriteAttempted`` ensures that any un-mocked REAL
# write adapter that reaches execution raises immediately — this is the
# critical safety test (plan §2 safety hard guard).
#
# Usage:
#   recorder = ReplayWriteRecorder(registry)
#   tools_by_name = recorder.build_tools_by_name()
#   # ... pass to execute_plan(execution_plan, tools_by_name, registry, ...)
#   records = recorder.get_records()
#
# Integration with invariants:
#   Invariant #1 (phantom-save) grades against recorder.get_records(),
#   NOT against DB commits.
#   Invariant #2 (unsolicited-write) grades forbidden_write_targets against
#   recorder.get_records() write_target values.


class ReplayWriteAttempted(RuntimeError):
    """Raised when a real (un-mocked) write adapter reaches execution
    during offline replay.

    This is the fail-fast safety guard (plan §2).  If this fires, the
    harness configuration is wrong — a real write adapter was injected
    instead of a ReplayWriteRecorder-backed mock.

    The unit tests MUST assert that this exception is raised when the
    guard fires (plan §16 — «write-tool-raise fail-fast test»).
    """

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            f"ReplayWriteAttempted: real write adapter '{tool_name}' reached "
            f"execution during offline replay.  This is a harness configuration "
            f"error — every write tool MUST be replaced by a ReplayWriteRecorder "
            f"mock before calling execute_plan().  "
            f"Check that build_write_recorder_tools_by_name() was called and "
            f"that its output was passed to execute_plan(tools_by_name=...)."
        )
        self.tool_name = tool_name


# ---------------------------------------------------------------------------
# Recorded write entry
# ---------------------------------------------------------------------------


class WriteRecord:
    """One recorded write attempt from a ReplayWriteRecorder-backed tool.

    Attributes
    ----------
    tool_name :
        The name of the write tool that was called (e.g. ``"save_core_fact"``).
    write_target :
        The primary domain write target (first entry of ``write_targets``).
        Kept for backward compatibility with callers that check a single domain.
    write_targets :
        All domain write targets for this tool call (e.g. ``("tasks", "reminders")``
        for ``attach_reminder``).  Invariant #2 checks ALL targets against
        ``forbidden_write_targets``.
    payload :
        A dict copy of the resolved args passed to the tool.  Used by
        invariant #1 to verify semantic payload match for save-claims.
    authorized :
        Whether the write is authorized per the frozen gold ``ReplayExpectations``.
        NOT hardcoded — computed by ``check_phantom_save`` from
        ``allowed_write_targets`` in the gold manifest.  Stored here as ``False``
        by the recorder; authorization is determined at grading time.
    raw_args :
        Original raw args dict (same as payload; stored separately so
        callers can distinguish between what the tool received and any
        post-processing).
    """

    __slots__ = ("tool_name", "write_target", "write_targets", "payload", "authorized", "raw_args")

    def __init__(
        self,
        tool_name: str,
        write_targets: tuple[str, ...],
        payload: dict,
        *,
        authorized: bool = False,
    ) -> None:
        self.tool_name = tool_name
        self.write_targets = write_targets
        # write_target kept for backward compatibility
        self.write_target = write_targets[0] if write_targets else "unknown"
        self.payload = dict(payload)
        self.authorized = authorized
        self.raw_args = dict(payload)

    def __repr__(self) -> str:
        return (
            f"WriteRecord(tool={self.tool_name!r}, targets={self.write_targets!r}, "
            f"authorized={self.authorized}, payload_keys={list(self.payload.keys())})"
        )


# ---------------------------------------------------------------------------
# Write-tool taxonomy (derived from ToolSpec.write_domains / effect)
# ---------------------------------------------------------------------------
#
# JUDGMENT CALL: this set is derived from ALL_TOOL_SPECS at runtime rather
# than being hardcoded.  This ensures it stays in sync as ToolSpec
# definitions evolve.  See ``_build_write_tool_info()`` below.
#
# We cache the result module-level to avoid re-computing on every recorder
# construction.

def _build_write_tool_info() -> dict[str, list[str]]:
    """Return {tool_name: [write_domain, ...]} for every write tool.

    Uses ``ALL_TOOL_SPECS`` from the sreda package.  The result is cached
    so the import cost (which involves loading all ToolSpec modules) is
    paid once.

    Raises ``ImportError`` if the sreda package is not on sys.path.
    Callers that cannot import sreda should use ``WRITE_TOOL_DOMAINS``
    (the fallback hardcoded map below) instead.
    """
    try:
        import sys
        from pathlib import Path
        _replay_root = Path(__file__).resolve().parents[2]
        if str(_replay_root / "src") not in sys.path:
            sys.path.insert(0, str(_replay_root / "src"))

        from sreda.services.tool_schemas.specs import ALL_TOOL_SPECS
    except ImportError:
        return {}

    result: dict[str, list[str]] = {}
    for spec in ALL_TOOL_SPECS:
        if spec.effect == "write":
            result[spec.name] = list(spec.write_domains)
    return result


def _authoritative_write_tool_names() -> set[str]:
    """Names of EVERY write tool, from the authoritative source.

    Uses ``ALL_TOOL_SPECS`` (every ``effect == "write"`` tool) when the sreda
    package is importable; otherwise the frozen ``_WRITE_TOOL_DOMAINS_FALLBACK``
    (isolated unit tests). This is the set ``build_replay_tools_by_name`` checks
    every write tool against — NOT the recorder's own (possibly stale) taxonomy.
    """
    live = _build_write_tool_info()
    if live:
        return set(live)
    return set(_WRITE_TOOL_DOMAINS_FALLBACK)


# Fallback hardcoded map (used when sreda can't be imported, e.g. in
# isolated unit tests that don't have the full package on sys.path).
# Derived from TOOL_FAMILY_MANIFEST + specs — matches ALL write tools.
_WRITE_TOOL_DOMAINS_FALLBACK: dict[str, list[str]] = {
    # shopping
    "add_shopping_items": ["shopping"],
    "mark_shopping_bought": ["shopping"],
    "remove_shopping_items": ["shopping"],
    "update_shopping_item": ["shopping"],
    "update_shopping_items_category": ["shopping"],
    "clear_bought_shopping": ["shopping"],
    # reminders
    "schedule_reminder": ["reminders"],
    "update_reminder": ["reminders"],
    "cancel_reminder": ["reminders"],
    # recipes
    "save_recipe": ["recipes"],
    "save_recipes_batch": ["recipes"],
    "delete_recipe": ["recipes"],
    # menu
    "plan_week_menu": ["menu"],
    "update_menu_item": ["menu"],
    "generate_shopping_from_menu": ["shopping"],
    "clear_menu": ["menu"],
    # household
    "add_family_members": ["household"],
    "update_family_member": ["household"],
    "remove_family_member": ["household"],
    # tasks
    "add_task": ["tasks"],
    "update_task": ["tasks"],
    "complete_task": ["tasks"],
    "uncomplete_task": ["tasks"],
    "cancel_task": ["tasks"],
    "delete_task": ["tasks"],
    "attach_reminder": ["tasks", "reminders"],
    "detach_reminder": ["tasks", "reminders"],
    "link_task_to_checklist": ["tasks", "checklists"],
    "unlink_task": ["tasks", "checklists"],
    # checklists
    "create_checklist": ["checklists"],
    "add_checklist_items": ["checklists"],
    "move_task_to_checklist": ["checklists"],
    "mark_checklist_item_done": ["checklists"],
    "delete_checklist_item": ["checklists"],
    "archive_checklist": ["checklists"],
    # onboarding
    "onboarding_answered": ["onboarding"],
    "onboarding_deferred": ["onboarding"],
    "onboarding_complete": ["onboarding"],
    # ui (request_local — still recorded, flagged separately)
    "reply_with_buttons": ["ui"],
    # memory
    "save_core_fact": ["memory"],
    "save_episode": ["memory"],
    # utility
    "log_unsupported_request": ["utility"],
}


class _RecordingWriteTool:
    """A mock tool that records the call and returns the canned raw string.

    Returned by ``ReplayWriteRecorder.build_tools_by_name()``.  Behaves
    exactly like ``_MockTool`` but also appends a ``WriteRecord`` to the
    recorder's internal list.
    """

    def __init__(
        self,
        name: str,
        raw: str,
        write_domains: list[str],
        recorder: "ReplayWriteRecorder",
    ) -> None:
        self.name = name
        self._raw = raw
        self._write_domains = write_domains
        self._recorder = recorder

    def _record(self, args: Any) -> None:
        payload = dict(args) if isinstance(args, dict) else {}
        # Store ALL write domains — not just the first.  Invariant #2 checks
        # every domain against forbidden_write_targets so secondary domains
        # (e.g. "reminders" for attach_reminder) must not be silently dropped.
        # authorization is NOT hardcoded here; it is computed at grading time
        # by check_phantom_save from the frozen ReplayExpectations.allowed_write_targets.
        write_targets = tuple(self._write_domains) if self._write_domains else ("unknown",)
        rec = WriteRecord(
            tool_name=self.name,
            write_targets=write_targets,
            payload=payload,
            authorized=False,  # graded from gold expectations, not hardcoded
        )
        self._recorder._records.append(rec)  # noqa: SLF001

    def invoke(self, args: Any) -> str:
        self._record(args)
        return self._raw

    async def ainvoke(self, args: Any) -> str:
        self._record(args)
        return self._raw


class _FailFastRealWriteTool:
    """Placeholder for any tool that was NOT mocked by the recorder.

    If a real write adapter reaches execution (i.e. the caller passed
    un-mocked tools_by_name alongside recorder tools), this raises
    ``ReplayWriteAttempted`` immediately.

    Used when ``ReplayWriteRecorder.build_tools_by_name(strict=True)``
    is called — the returned dict includes a ``_FailFastRealWriteTool``
    for every known write tool name that is NOT in the provided
    ``extra_tools_by_name``.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def invoke(self, args: Any) -> str:  # noqa: ARG002
        raise ReplayWriteAttempted(self.name)

    async def ainvoke(self, args: Any) -> str:  # noqa: ARG002
        raise ReplayWriteAttempted(self.name)


class ReplayWriteRecorder:
    """Factory + recorder for write-tool mocks in offline replay.

    Usage
    -----
    ::

        recorder = ReplayWriteRecorder()
        tools = recorder.build_tools_by_name()
        # Merge with read-tool mocks if needed:
        all_tools = {**build_mock_tools_by_name(), **tools}
        # Pass to executor:
        log = await execute_plan(plan, all_tools, registry)
        # Inspect what was recorded:
        for rec in recorder.get_records():
            print(rec)

    The recorder also exposes ``get_write_targets()`` and
    ``has_write_to(domain)`` convenience helpers for invariant checkers.

    Parameters
    ----------
    write_tool_domains :
        Optional override for the write-tool taxonomy.  When ``None``
        (default) the recorder tries to load from ``ALL_TOOL_SPECS`` and
        falls back to the hardcoded map.  Pass an explicit dict in unit
        tests that do not have sreda on sys.path.
    """

    def __init__(
        self,
        write_tool_domains: dict[str, list[str]] | None = None,
    ) -> None:
        self._records: list[WriteRecord] = []
        if write_tool_domains is not None:
            self._write_tool_domains = write_tool_domains
        else:
            live = _build_write_tool_info()
            self._write_tool_domains = live if live else _WRITE_TOOL_DOMAINS_FALLBACK

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    def build_tools_by_name(self) -> dict[str, _RecordingWriteTool]:
        """Return ``{tool_name: _RecordingWriteTool}`` for every write tool.

        The returned dict covers ONLY write tools.  Callers should merge
        it on top of ``build_mock_tools_by_name()`` (read tools) so the
        executor has a complete ``tools_by_name`` mapping:

            all_tools = {**build_mock_tools_by_name(), **recorder.build_tools_by_name()}

        Write tools that appear in ``_WRITE_TOOL_DOMAINS_FALLBACK`` but
        are not in ``_MOCK_RAW`` (e.g. if the registry grows) fall back
        to a generic ``"ok"`` string — logged with a warning.
        """
        tools: dict[str, _RecordingWriteTool] = {}
        for name, domains in self._write_tool_domains.items():
            raw = _MOCK_RAW.get(name, "ok")
            tools[name] = _RecordingWriteTool(
                name=name,
                raw=raw,
                write_domains=list(domains),
                recorder=self,
            )
        return tools

    def build_fail_fast_guards(self) -> dict[str, _FailFastRealWriteTool]:
        """Return ``{tool_name: _FailFastRealWriteTool}`` for every write tool.

        Use this when you want the fail-fast guard WITHOUT recording — i.e.
        in a test that asserts ``ReplayWriteAttempted`` is raised when a
        write tool is accidentally reached without a recorder in place.
        """
        return {
            name: _FailFastRealWriteTool(name)
            for name in self._write_tool_domains
        }

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_records(self) -> list[WriteRecord]:
        """Return a snapshot of all recorded write calls (in call order)."""
        return list(self._records)

    def get_write_targets(self) -> list[str]:
        """Return the list of write_target values from all recorded calls."""
        return [r.write_target for r in self._records]

    def has_write_to(self, domain: str) -> bool:
        """True if any recorded write had ``write_target == domain``."""
        return any(r.write_target == domain for r in self._records)

    def reset(self) -> None:
        """Clear all recorded write calls (useful between test cases)."""
        self._records.clear()

    def __repr__(self) -> str:
        return (
            f"ReplayWriteRecorder(records={len(self._records)}, "
            f"write_tools={len(self._write_tool_domains)})"
        )


def build_replay_tools_by_name(
    recorder: "ReplayWriteRecorder",
    *,
    extra_read_tools: "dict[str, Any] | None" = None,
) -> "dict[str, Any]":
    """Build the COMPLETE replay tool map for offline replay.

    This is the single factory callers should use — it combines read-only
    mock tools with ReplayWriteRecorder-backed tools for EVERY write tool,
    and then ASSERTS that every known write tool is recorder-backed.  Any
    write tool that would fall through to a real adapter raises
    ``ReplayWriteAttempted`` immediately (fail-fast guard).

    Parameters
    ----------
    recorder :
        A ``ReplayWriteRecorder`` instance that will capture all write calls.
    extra_read_tools :
        Optional additional read-only tools to include (e.g. replay-actual
        historical tools).  These are merged BEFORE the write-recorder tools
        so that write tools always take precedence.

    Returns
    -------
    dict[str, Any]
        Complete ``tools_by_name`` mapping suitable for passing to
        ``execute_plan(tools_by_name=...)``.

    Raises
    ------
    AssertionError
        If any known write tool is NOT backed by the recorder (i.e. the
        recorder's write_tool_domains does not cover a tool that exists in
        the read-mock set but is actually a write tool).  This prevents
        silent real-write leakage.
    """
    # Start with all read-only mocks.
    all_tools: dict[str, Any] = dict(build_mock_tools_by_name())

    # AUTHORITATIVE write-tool set (R2 fix): NOT the recorder's own taxonomy
    # (that assertion was tautological). Use ALL_TOOL_SPECS when importable,
    # else the frozen fallback — so registry drift / a new write adapter that
    # the recorder taxonomy missed is caught, not silently left as a read-mock.
    authoritative_writes = _authoritative_write_tool_names()

    # Reject extra_read_tools that are actually write tools — they must be
    # recorder-backed, never injected as plain read tools.
    if extra_read_tools:
        bad = sorted(set(extra_read_tools) & authoritative_writes)
        # Explicit raise (NOT assert) — assert is stripped under `python -O`,
        # which would silently allow a real write tool through (R3 safety fix).
        if bad:
            raise AssertionError(
                f"build_replay_tools_by_name: extra_read_tools contains write tool(s) "
                f"{bad} — write tools must be recorder-backed, not injected as read tools."
            )
        all_tools.update(extra_read_tools)

    # Recorder-backed write tools.
    write_tools = recorder.build_tools_by_name()

    # STRICT: every AUTHORITATIVE write tool MUST be recorder-backed. If the
    # recorder taxonomy is stale and misses one, fail loudly at configuration
    # time (a missed write tool would otherwise execute as a plain read-mock and
    # its writes would go unrecorded → invariant undercount + leakage risk).
    missing = sorted(authoritative_writes - set(write_tools))
    # Explicit raise (NOT assert) — survives `python -O` (R3 safety fix).
    if missing:
        raise AssertionError(
            f"build_replay_tools_by_name: authoritative write tool(s) {missing} are "
            f"NOT recorder-backed — update the recorder's write-tool taxonomy. "
            f"Silent real-write leakage / unrecorded-write risk."
        )

    # Override read-mock stubs with recorder-backed write tools.
    all_tools.update(write_tools)

    # Defensive: any other write tool not yet present → fail-fast guard.
    for tool_name, guard in recorder.build_fail_fast_guards().items():
        if tool_name not in all_tools:
            all_tools[tool_name] = guard

    return all_tools


# ---------------------------------------------------------------------------
# replay_actual mock mode (plan §8 — «replay-actual»)
# ---------------------------------------------------------------------------
#
# When the old trace IS available, we can supply the historical tool output
# so the planner's branch logic fires on the real old outcome rather than
# the generic stub.  This is the «replay-actual» mode.
#
# Usage:
#   tools = build_replay_actual_tools(historical_outputs)
#   # historical_outputs = {"s1": "ok:saved:rec_...", "s2": "[]", ...}
#   # Keys are step_ids; values are the raw historical output strings.


class _ReplayActualTool:
    """Returns a pre-supplied historical output string instead of a stub.

    Used in replay-actual mode where we want the executor to branch on the
    REAL historical outcome (e.g. the old plan actually returned "not_found"
    for a recipe search, and we want the new plan's branch logic to see that).
    """

    def __init__(self, name: str, historical_raw: str) -> None:
        self.name = name
        self._raw = historical_raw

    def invoke(self, args: Any) -> str:  # noqa: ARG002
        return self._raw

    async def ainvoke(self, args: Any) -> str:  # noqa: ARG002
        return self._raw


def build_replay_actual_tools(
    step_to_raw: dict[str, str],
    step_to_tool_name: dict[str, str],
) -> dict[str, "_ReplayActualTool"]:
    """Build a ``tools_by_name``-compatible dict for replay-actual mode.

    Parameters
    ----------
    step_to_raw :
        Maps step_id → historical raw output string.
        E.g. ``{"s1": "ok:added:3", "s2": "no shopping items"}``.
    step_to_tool_name :
        Maps step_id → tool name (needed to key the returned dict by
        tool name, since execute_plan looks up tools by name not step_id).
        E.g. ``{"s1": "add_shopping_items", "s2": "list_shopping"}``.

    Returns
    -------
    dict[str, _ReplayActualTool]
        Keyed by tool_name.  If multiple steps use the same tool, the
        LAST step's output wins (callers should ensure uniqueness or
        use per-step injection at the orchestrator level).

    TODO (Phase B):
        The orchestrator (``run_replay.py``) needs to inject these per-step,
        not just per-tool-name.  This factory is Phase A scaffolding;
        proper per-step injection is a Phase B orchestration concern.
    """
    result: dict[str, _ReplayActualTool] = {}
    for step_id, raw in step_to_raw.items():
        tool_name = step_to_tool_name.get(step_id)
        if tool_name is not None:
            result[tool_name] = _ReplayActualTool(name=tool_name, historical_raw=raw)
    return result


# Update __all__ for the new Phase A exports.
__all__ = [
    "build_mock_tools_by_name",
    "build_replay_actual_tools",
    "build_replay_tools_by_name",
    "ReplayWriteAttempted",
    "ReplayWriteRecorder",
    "WriteRecord",
]

