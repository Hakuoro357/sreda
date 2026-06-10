"""#115 — add_shopping_items returns by-name outcome buckets (red-before-impl).

Statuses: added (created>0) / empty (all-duplicate) / replay (okv2-only — the
same step's earlier retry already inserted the rows via the per-item op_id
ON CONFLICT; idempotent, NOT a user-facing duplicate). Wire→parser→model→
presenter tests always run; service-level bucket-attribution tests skip when
the env lacks pymorphy3 (run in CI).
"""

from __future__ import annotations

import pytest

from sreda.services.composer.presenters import (
    build_display_field_map,
    render_display_text,
    set_display_field_map,
)
from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.housewife import (
    AddShoppingItemsAdded,
    AddShoppingItemsEmpty,
    AddShoppingItemsReplay,
    parse_add_shopping_items,
)
from sreda.services.tool_schemas.specs import ALL_TOOL_SPECS
from sreda.services.tool_schemas.specs_shopping import ADD_SHOPPING_ITEMS_SPEC
from sreda.services.tool_schemas.tool_ok_codec import encode_tool_ok

SH1 = "sh_" + "a" * 24
SH2 = "sh_" + "b" * 24


@pytest.fixture(autouse=True)
def _install_display_map():
    set_display_field_map(build_display_field_map(ALL_TOOL_SPECS))
    yield
    set_display_field_map({})


def _added(**over):
    payload = {
        "added_count": 1, "item_ids": [SH1], "created": ["молоко"],
        "duplicates_existing": [], "duplicates_in_batch": [],
        "replayed": [], "invalid": [], "duplicate_item_ids": [],
    }
    payload.update(over)
    return encode_tool_ok("added", payload)


# --- added --------------------------------------------------------------------


def test_added_okv2_names_all_groups():
    raw = _added(
        added_count=1, item_ids=[SH1], created=["молоко"],
        duplicates_existing=["хлеб"], duplicate_item_ids=[SH2],
        duplicates_in_batch=["молоко"],
    )
    parsed = parse_add_shopping_items(raw)
    assert isinstance(parsed, AddShoppingItemsAdded)
    assert parsed.created == ["молоко"]
    assert parsed.duplicates_existing == ["хлеб"]
    s = parsed.display_summary
    assert "Добавила" in s and "молоко" in s and "Уже было" in s and "хлеб" in s
    assert SH1 not in s and SH2 not in s


def test_added_presenter_shows_names():
    parsed = parse_add_shopping_items(_added())
    text = render_display_text(
        "add_shopping_items", parsed.model_dump(), domain_status="added"
    )
    assert "молоко" in text and SH1 not in text


def test_added_count_must_match_created_names():
    # the planner-path lie (duplicates inflating added_count) must fail closed
    bad = _added(added_count=2, item_ids=[SH1, SH2], created=["молоко"])
    assert isinstance(parse_add_shopping_items(bad), ToolOutputContractViolation)


def test_added_dup_ids_must_match_dup_names():
    bad = _added(duplicates_existing=["хлеб"], duplicate_item_ids=[])
    assert isinstance(parse_add_shopping_items(bad), ToolOutputContractViolation)


def test_added_blank_name_fails_closed():
    bad = _added(created=["  "])
    assert isinstance(parse_add_shopping_items(bad), ToolOutputContractViolation)


# --- empty (all-duplicate) -----------------------------------------------------


def test_empty_okv2_carries_duplicate_names():
    raw = encode_tool_ok(
        "empty",
        {"added_count": 0, "duplicates_existing": ["хлеб"],
         "duplicates_in_batch": [], "duplicate_item_ids": [SH2]},
    )
    parsed = parse_add_shopping_items(raw)
    assert isinstance(parsed, AddShoppingItemsEmpty)
    assert "Уже было" in parsed.display_summary and "хлеб" in parsed.display_summary


def test_empty_okv2_without_names_fails_closed():
    # okv2 empty MUST name the duplicates (legacy ok:added:0 is exempt)
    raw = encode_tool_ok(
        "empty",
        {"added_count": 0, "duplicates_existing": [],
         "duplicates_in_batch": [], "duplicate_item_ids": []},
    )
    assert isinstance(parse_add_shopping_items(raw), ToolOutputContractViolation)


# --- replay (okv2-only, legacy_compat=no) --------------------------------------


def test_replay_okv2_names_and_ids():
    raw = encode_tool_ok(
        "replay",
        {"replayed": ["молоко", "хлеб"], "item_ids": [SH1, SH2],
         "duplicates_existing": [], "duplicates_in_batch": [],
         "duplicate_item_ids": []},
    )
    parsed = parse_add_shopping_items(raw)
    assert isinstance(parsed, AddShoppingItemsReplay)
    s = parsed.display_summary
    assert "Уже добавляла раньше" in s and "молоко" in s
    assert "дубл" not in s.lower()  # replay is NOT presented as a duplicate


def test_replay_ids_names_mismatch_fails_closed():
    raw = encode_tool_ok(
        "replay",
        {"replayed": ["молоко"], "item_ids": [SH1, SH2],
         "duplicates_existing": [], "duplicates_in_batch": [],
         "duplicate_item_ids": []},
    )
    assert isinstance(parse_add_shopping_items(raw), ToolOutputContractViolation)


def test_replay_is_committed_status():
    # #115: replay rows EXIST (inserted by an earlier retry of the same step) —
    # recovery must treat the durable write as committed.
    assert "replay" in ADD_SHOPPING_ITEMS_SPEC.committed_statuses
    assert "added" in ADD_SHOPPING_ITEMS_SPEC.committed_statuses


# --- legacy --------------------------------------------------------------------


def test_legacy_positional_still_parses():
    p1 = parse_add_shopping_items(f"ok:added:2:ids=[{SH1},{SH2}]")
    assert isinstance(p1, AddShoppingItemsAdded)
    assert p1.added_count == 2 and p1.created == []
    assert p1.display_summary == "Готово."
    p2 = parse_add_shopping_items("ok:added:0")
    assert isinstance(p2, AddShoppingItemsEmpty)
    assert p2.display_summary == "Готово."


def test_malformed_okv2_sentinel():
    assert isinstance(
        parse_add_shopping_items("okv2:added:{bad"), ToolOutputContractViolation
    )


def test_missing_bucket_keys_fail_closed():
    # Codex #115 [MAJOR]: okv2 producers must send EVERY bucket key per status.
    raw = encode_tool_ok(
        "added", {"added_count": 1, "item_ids": [SH1], "created": ["молоко"]},
    )  # buckets omitted
    assert isinstance(parse_add_shopping_items(raw), ToolOutputContractViolation)
    raw2 = encode_tool_ok("empty", {"added_count": 0})
    assert isinstance(parse_add_shopping_items(raw2), ToolOutputContractViolation)


# --- service-level bucket attribution (CI; local env may lack pymorphy3) -------


def test_service_legacy_path_buckets(db_session):
    pytest.importorskip("pymorphy3")
    from sreda.services.housewife_shopping import HousewifeShoppingService

    svc = HousewifeShoppingService(db_session)
    svc.add_items(tenant_id="t1", user_id="u1", items=[{"title": "хлеб"}])
    out = svc.add_items_detailed(
        tenant_id="t1", user_id="u1",
        items=[{"title": "молоко"}, {"title": "хлеб"}, {"title": " молоко "}],
    )
    assert [r.title for r in out.created] == ["молоко"]
    assert out.duplicates_existing == ["хлеб"]          # cross-call (name of the row)
    assert out.duplicates_in_batch == ["молоко"]        # in-batch repeat
    assert out.replayed == []
    assert out.ordered_rows == out.created              # legacy shape: created only


def test_service_planner_path_replay(db_session):
    pytest.importorskip("pymorphy3")
    from sreda.runtime.planner.tool_runtime import (
        ToolRuntimeContext,
        bind_tool_runtime,
    )
    from sreda.services.housewife_shopping import HousewifeShoppingService

    svc = HousewifeShoppingService(db_session)
    ctx = ToolRuntimeContext(
        operation_id="op_exec1_s1", execution_id="exec1", step_id="s1",
        tool_name="add_shopping_items", tenant_id="t1", user_id=None,
    )
    with bind_tool_runtime(ctx):
        first = svc.add_items_detailed(
            tenant_id="t1", user_id="u1", items=[{"title": "молоко"}]
        )
    assert [r.title for r in first.created] == ["молоко"] and first.replayed == []

    # Same step retried (same ctx) — the pending row carries THIS step's
    # per-item operation_id, so it MUST classify as REPLAY (committed), never
    # as a user-facing duplicate / empty (Codex #115 CRITICAL: empty sits
    # outside committed_statuses and recovery would treat the durable write
    # as never committed).
    db_session.expire_all()
    with bind_tool_runtime(ctx):
        second = svc.add_items_detailed(
            tenant_id="t1", user_id="u1", items=[{"title": "молоко"}]
        )
    assert second.created == []
    assert [r.title for r in second.replayed] == ["молоко"]
    assert second.duplicates_existing == []
    assert [r.title for r in second.ordered_rows] == ["молоко"]  # planner ref intact

    # A DIFFERENT step adding the same pending title IS a user-facing duplicate.
    ctx2 = ToolRuntimeContext(
        operation_id="op_exec1_s2", execution_id="exec1", step_id="s2",
        tool_name="add_shopping_items", tenant_id="t1", user_id=None,
    )
    db_session.expire_all()
    with bind_tool_runtime(ctx2):
        third = svc.add_items_detailed(
            tenant_id="t1", user_id="u1", items=[{"title": "молоко"}]
        )
    assert third.created == [] and third.replayed == []
    assert third.duplicates_existing == ["молоко"]
