"""Regression tests for the 2026-07-18 audit fixes — tool-schemas scope.

Covers (audit report: plans/audit-2026-07-18/tool-schemas-review.md):

1. MAJOR #1 — ``update_family_member`` now declares
   ``required_any_non_null_args`` (refs-present no-op guard, same
   pattern as shopping/reminders/tasks) and ``clear_birth_year`` is
   ``Literal[True] | None`` so ``False`` can't slip past the guard.
2. MINOR #2 — ``AddShoppingItemsInput.items`` has an upper bound.
3. MINOR #3 — ``FetchUrl`` schema delegates to ``ssrf_guard`` (#244):
   obfuscated numeric hosts rejected at planner-input time.
4. MINOR #4 — ``dispatch_typed_output`` caches the ``TypeAdapter``.
5. MINOR #5 — ``ListChecklistItemsOk.display_summary`` keeps the inner
   «»-boundaries between item and list titles.
6. MINOR #6 — ``ScheduleReminderScheduled.trigger_at_iso`` uses the
   strict ``TriggerIso`` alias (fail-closed on legacy garbage).
7. MINOR #9 — ``common.__all__`` exports ``ChecklistItemId`` /
   ``FamilyMemberId``; MINOR #13 — ``title_match`` fields are capped
   via ``TitleMatch``.
8. MINOR #12 — ``REACT_ONLY_TOOLS ⊆ TOOL_FAMILY_MANIFEST`` is enforced
   at import time.

No network, no PostgreSQL.
"""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from sreda.services.tool_schemas import common
from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.executor_contract import (
    _ADAPTER_CACHE,
    _get_output_adapter,
    dispatch_typed_output,
)
from sreda.services.tool_schemas.families import (
    REACT_ONLY_TOOLS,
    TOOL_FAMILY_MANIFEST,
)
from sreda.services.tool_schemas.housewife import (
    ListChecklistItemsOk,
    ListChecklistItemsRow,
    ScheduleReminderScheduled,
    ScheduleReminderSkippedPast,
    parse_tool_output,
)
from sreda.services.tool_schemas.specs_household import (
    UPDATE_FAMILY_MEMBER_SPEC,
    UpdateFamilyMemberInput,
)
from sreda.services.tool_schemas.specs_reminders import ListRemindersInput
from sreda.services.tool_schemas.specs_shopping import (
    AddShoppingItemsInput,
    ListShoppingInput,
)
from sreda.services.tool_schemas.specs_tasks import ListTasksInput
from sreda.services.tool_schemas.specs_web import FetchUrlToolInput

FM_A = "fm_aaaaaaaaaaaaaaaaaaaaaaaa"
REM_A = "rem_aaaaaaaaaaaaaaaaaaaaaaaa"


def _member_ref_args(**extra: object) -> dict:
    return {"member_id": "${s1.members.only.member_id}", **extra}


# ---------------------------------------------------------------------------
# 1. MAJOR — update_family_member refs-present no-op guard
# ---------------------------------------------------------------------------


def test_update_family_member_declares_required_any_non_null_args() -> None:
    assert UPDATE_FAMILY_MEMBER_SPEC.required_any_non_null_args == [
        "name",
        "role",
        "birth_year",
        "clear_birth_year",
        "age_hint",
        "notes",
    ]


def test_update_family_member_refs_only_call_rejected() -> None:
    """Plan-shape ``{"member_id": "${s1...}"}`` with zero mutable fields
    must be rejected by the static validator layer (previously passed
    plan validation and shipped a silent no-op returning ok:updated)."""
    with pytest.raises(ValueError, match="at least one of"):
        UPDATE_FAMILY_MEMBER_SPEC.validate_args_satisfy_required_any(
            _member_ref_args()
        )
    # Explicit nulls don't count either.
    with pytest.raises(ValueError, match="at least one of"):
        UPDATE_FAMILY_MEMBER_SPEC.validate_args_satisfy_required_any(
            _member_ref_args(name=None, notes=None)
        )


def test_update_family_member_refs_with_mutable_field_accepted() -> None:
    UPDATE_FAMILY_MEMBER_SPEC.validate_args_satisfy_required_any(
        _member_ref_args(notes="${s1.members.only.notes}")
    )
    UPDATE_FAMILY_MEMBER_SPEC.validate_args_satisfy_required_any(
        _member_ref_args(age_hint="")  # empty string = clear intent
    )
    UPDATE_FAMILY_MEMBER_SPEC.validate_args_satisfy_required_any(
        _member_ref_args(clear_birth_year=True)
    )


def test_update_family_member_clear_birth_year_false_rejected() -> None:
    """``Literal[True] | None``: False is a validation error now — it
    used to count as a non-null «provided» field while the runtime
    branches only on True."""
    with pytest.raises(ValidationError):
        UpdateFamilyMemberInput.model_validate(
            {"member_id": FM_A, "clear_birth_year": False, "name": "Катя"}
        )


def test_update_family_member_clear_birth_year_true_still_works() -> None:
    parsed = UpdateFamilyMemberInput.model_validate(
        {"member_id": FM_A, "clear_birth_year": True}
    )
    assert parsed.clear_birth_year is True
    # ... and stays mutually exclusive with birth_year=N.
    with pytest.raises(ValidationError, match="mutually exclusive"):
        UpdateFamilyMemberInput.model_validate(
            {"member_id": FM_A, "clear_birth_year": True, "birth_year": 1990}
        )


def test_update_family_member_fully_literal_noop_still_rejected() -> None:
    """The model-level guard keeps working for the fully-literal path."""
    with pytest.raises(ValidationError, match="at least one"):
        UpdateFamilyMemberInput.model_validate({"member_id": FM_A})


# ---------------------------------------------------------------------------
# 2. MINOR — AddShoppingItemsInput.items upper bound
# ---------------------------------------------------------------------------


def test_add_shopping_items_batch_capped() -> None:
    item = {"title": "молоко"}
    ok = AddShoppingItemsInput.model_validate({"items": [item] * 100})
    assert len(ok.items) == 100
    with pytest.raises(ValidationError):
        AddShoppingItemsInput.model_validate({"items": [item] * 101})


# ---------------------------------------------------------------------------
# 3. MINOR — FetchUrl delegates to ssrf_guard (#244)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.1/",                # short-form IPv4
        "http://2130706433/",           # decimal-int host
        "http://0x7f.0.0.1/",           # hex-dotted host
        "http://0177.0.0.1/",           # octal-dotted host
        "http://example.com:8080/",     # non-80/443 port (runtime rejects)
        "http://user@example.com/",     # userinfo (runtime rejects)
        "http://foo.localhost/",        # *.localhost family
        "http://[::ffff:127.0.0.1]/",   # IPv4-mapped IPv6 loopback
    ],
)
def test_fetch_url_rejects_obfuscated_or_runtime_blocked_hosts(url: str) -> None:
    """Pre-fix the schema mirror only caught canonical IP literals and
    let every one of these through (runtime guard would still block
    them — the schema now mirrors that policy via ssrf_guard)."""
    with pytest.raises(ValidationError):
        FetchUrlToolInput.model_validate({"url": url})


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/article",
        "https://example.com:443/a",
        "http://8.8.8.8/",  # public IP literal is allowed by the runtime guard
    ],
)
def test_fetch_url_still_accepts_public_urls(url: str) -> None:
    assert FetchUrlToolInput.model_validate({"url": url}).url == url


# ---------------------------------------------------------------------------
# 4. MINOR — TypeAdapter cache in dispatch_typed_output
# ---------------------------------------------------------------------------


def test_dispatch_typed_output_caches_adapter() -> None:
    spec_model = UPDATE_FAMILY_MEMBER_SPEC.output_model
    _ADAPTER_CACHE.pop(spec_model, None)
    first = _get_output_adapter(spec_model)
    second = _get_output_adapter(spec_model)
    assert first is second
    # And the public boundary path keeps working through the cache
    # (legacy bare "ok:updated" → UpdateFamilyMemberOk with name=None).
    out = dispatch_typed_output("update_family_member", "ok:updated", spec_model)
    assert type(out).__name__ == "UpdateFamilyMemberOk"
    assert _get_output_adapter(spec_model) is first


# ---------------------------------------------------------------------------
# 5. MINOR — ListChecklistItemsOk.display_summary keeps inner «»
# ---------------------------------------------------------------------------


def _cl_row(suffix: str, title: str, status: str, list_title: str) -> ListChecklistItemsRow:
    return ListChecklistItemsRow(
        item_id=f"clitem_{suffix * 24}",
        item_title=title,
        item_status=status,
        list_id=f"checklist_{suffix * 24}",
        list_title=list_title,
    )


def test_list_checklist_items_display_summary_preserves_boundaries() -> None:
    model = ListChecklistItemsOk(
        items=[_cl_row("a", "молоко", "pending", "дача")]
    )
    assert model.display_summary == "Пункты: «молоко» в списке «дача»."


def test_list_checklist_items_display_summary_dedup_suffix() -> None:
    model = ListChecklistItemsOk(
        items=[
            _cl_row("a", "молоко", "pending", "дача"),
            _cl_row("b", "молоко", "done", "дача"),
        ]
    )
    assert model.display_summary == (
        "Пункты: «молоко» в списке «дача», «молоко» в списке «дача» (#2 ✓)."
    )


def test_list_checklist_items_display_summary_sanitizes_and_caps() -> None:
    # Forged guillemets in user data are stripped (no boundary escape:
    # the «» , «» forgery would fake a name separator for the voice),
    # and the list is capped with an exact «и ещё N» remainder.
    model = ListChecklistItemsOk(
        items=[
            _cl_row("a", "молоко» , «хлеб", "pending", "да«ча"),
            *(_cl_row("b", f"пункт{i}", "pending", "дача") for i in range(11)),
        ]
    )
    summary = model.display_summary
    assert "» , «" not in summary  # forged boundary gone
    assert "«молоко , хлеб» в списке «дача»" in summary  # sanitized, wrapped
    assert summary.count("и ещё 2") == 1


# ---------------------------------------------------------------------------
# 6. MINOR — ScheduleReminderScheduled.trigger_at_iso strict alias
# ---------------------------------------------------------------------------


def test_schedule_reminder_garbage_iso_fails_closed() -> None:
    parsed = parse_tool_output(
        "schedule_reminder", f"ok:scheduled:{REM_A}:garbage"
    )
    assert isinstance(parsed, ToolOutputContractViolation)


def test_schedule_reminder_real_iso_parses() -> None:
    parsed = parse_tool_output(
        "schedule_reminder",
        f"ok:scheduled:{REM_A}:2026-07-18T15:00:00+00:00",
    )
    assert isinstance(parsed, ScheduleReminderScheduled)
    assert parsed.trigger_at_iso == "2026-07-18T15:00:00+00:00"


def test_schedule_reminder_skipped_past_loose_path_untouched() -> None:
    """Legacy echoes the RAW planner trigger_iso here (may be naive /
    seconds-less) — intentionally NOT tightened."""
    parsed = parse_tool_output(
        "schedule_reminder", "skipped:past:2026-07-18T15:00:late_by_5min"
    )
    assert isinstance(parsed, ScheduleReminderSkippedPast)
    assert parsed.late_by_minutes == 5


# ---------------------------------------------------------------------------
# 7. MINOR — common.__all__ completeness + title_match caps
# ---------------------------------------------------------------------------


def test_common_all_exports_id_aliases_and_title_match() -> None:
    assert "ChecklistItemId" in common.__all__
    assert "FamilyMemberId" in common.__all__
    assert "TitleMatch" in common.__all__
    namespace: dict = {}
    exec(
        "from sreda.services.tool_schemas.common import *",
        {"__name__": "common_star_test"},
        namespace,
    )
    assert "ChecklistItemId" in namespace
    assert "FamilyMemberId" in namespace


@pytest.mark.parametrize(
    "model",
    [ListShoppingInput, ListRemindersInput, ListTasksInput],
)
def test_title_match_capped_and_stripped(model) -> None:
    adapter = TypeAdapter(model)
    parsed = adapter.validate_python({"title_match": "  молоко  "})
    assert parsed.title_match == "молоко"
    with pytest.raises(ValidationError):
        adapter.validate_python({"title_match": "x" * 201})
    with pytest.raises(ValidationError):
        adapter.validate_python({"title_match": "   "})


# ---------------------------------------------------------------------------
# 8. MINOR — REACT_ONLY_TOOLS ⊆ TOOL_FAMILY_MANIFEST (import-time gate)
# ---------------------------------------------------------------------------


def test_react_only_tools_subset_of_manifest() -> None:
    assert set(REACT_ONLY_TOOLS) <= set(TOOL_FAMILY_MANIFEST)
