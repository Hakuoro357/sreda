"""#115 — save_recipes_batch returns by-name outcome buckets (red-before-impl).

Batch outcome-object pattern: created / duplicates_existing / duplicates_in_batch /
invalid, each BY NAME (was count/id-only — Sub-A4 drop). Tested at the wire→parser→
model→presenter surface (the #74 deliverable); the okv2 wire string is exactly what
the chat-tool emits.
"""

from __future__ import annotations

import pytest

from sreda.services.composer.presenters import (
    build_display_field_map,
    render_display_text,
    set_display_field_map,
)
from sreda.services.tool_schemas.base import ToolOutputContractViolation
from sreda.services.tool_schemas.executor_contract import dispatch_typed_output
from sreda.services.tool_schemas.housewife import (
    SaveRecipesBatchOk,
    SaveRecipesBatchOutput,
    parse_save_recipes_batch,
)
from sreda.services.tool_schemas.specs import ALL_TOOL_SPECS
from sreda.services.tool_schemas.tool_ok_codec import encode_tool_ok

R1 = "rec_" + "a" * 24
R2 = "rec_" + "b" * 24


def _wire(**payload) -> str:
    base = {
        "created_count": len(payload.get("created", [])),
        "skipped_as_duplicate": len(payload.get("duplicates_existing", [])),
        "recipe_ids": payload.pop("recipe_ids", []),
        "created": [],
        "duplicates_existing": [],
        "duplicates_in_batch": [],
        "invalid": [],
    }
    base.update(payload)
    base["created_count"] = len(base["created"])
    base["skipped_as_duplicate"] = len(base["duplicates_existing"])
    return encode_tool_ok("batch_saved", base)


@pytest.fixture(autouse=True)
def _install_display_map():
    set_display_field_map(build_display_field_map(ALL_TOOL_SPECS))
    yield
    set_display_field_map({})


def test_parse_okv2_groups_by_name():
    raw = _wire(
        created=["Борщ", "Плов"],
        recipe_ids=[R1, R2],
        duplicates_existing=["Окрошка"],
        duplicates_in_batch=["Борщ"],
        invalid=["БезИсточника"],
    )
    parsed = parse_save_recipes_batch(raw)
    assert isinstance(parsed, SaveRecipesBatchOk)
    assert parsed.created == ["Борщ", "Плов"]
    assert parsed.duplicates_existing == ["Окрошка"]
    assert parsed.duplicates_in_batch == ["Борщ"]
    assert parsed.invalid == ["БезИсточника"]
    assert parsed.created_count == 2
    assert parsed.recipe_ids == [R1, R2]


def test_display_summary_shows_all_groups_by_name():
    raw = _wire(
        created=["Борщ"], recipe_ids=[R1], duplicates_existing=["Окрошка"]
    )
    parsed = parse_save_recipes_batch(raw)
    s = parsed.display_summary
    assert "Борщ" in s and "Окрошка" in s
    assert R1 not in s  # ids never leak
    assert "Сохранила" in s and "Уже было" in s


def test_dispatch_round_trip():
    raw = _wire(created=["Плов"], recipe_ids=[R1])
    model = dispatch_typed_output("save_recipes_batch", raw, SaveRecipesBatchOutput)
    assert isinstance(model, SaveRecipesBatchOk)
    assert "Плов" in model.model_dump()["display_summary"]


def test_presenter_shows_names():
    raw = _wire(created=["Гуляш", "Рагу"], recipe_ids=[R1, R2])
    parsed = parse_save_recipes_batch(raw)
    text = render_display_text(
        "save_recipes_batch", parsed.model_dump(), domain_status="batch_saved"
    )
    assert "Гуляш" in text and "Рагу" in text
    assert R1 not in text


def test_legacy_positional_still_parses_without_names():
    parsed = parse_save_recipes_batch(
        f"ok:batch_saved:2:skipped_as_duplicate:1:ids=[{R1},{R2}]"
    )
    assert isinstance(parsed, SaveRecipesBatchOk)
    assert parsed.created_count == 2
    assert parsed.created == []  # legacy carried no names


def test_malformed_okv2_is_contract_violation():
    parsed = parse_save_recipes_batch("okv2:batch_saved:{bad json")
    assert isinstance(parsed, ToolOutputContractViolation)


def test_created_names_count_mismatch_fails_closed():
    # okv2 names must match counts (Codex #115 MAJOR): created_count=2 but 1 name
    raw = encode_tool_ok(
        "batch_saved",
        {
            "created_count": 2,
            "skipped_as_duplicate": 0,
            "recipe_ids": [R1, R2],
            "created": ["Борщ"],  # only 1 name for count=2
            "duplicates_existing": [],
            "duplicates_in_batch": [],
            "invalid": [],
        },
    )
    assert isinstance(parse_save_recipes_batch(raw), ToolOutputContractViolation)


def test_duplicates_names_count_mismatch_fails_closed():
    raw = encode_tool_ok(
        "batch_saved",
        {
            "created_count": 0,
            "skipped_as_duplicate": 2,
            "recipe_ids": [],
            "created": [],
            "duplicates_existing": ["Окрошка"],  # 1 name for skipped=2
            "duplicates_in_batch": [],
            "invalid": [],
        },
    )
    assert isinstance(parse_save_recipes_batch(raw), ToolOutputContractViolation)


def test_blank_name_fails_closed():
    # Codex #115 R2 [MAJOR]: blank bucket name → fail closed (voice would drop it)
    raw = encode_tool_ok(
        "batch_saved",
        {
            "created_count": 1,
            "skipped_as_duplicate": 0,
            "recipe_ids": [R1],
            "created": ["  "],  # blank
            "duplicates_existing": [],
            "duplicates_in_batch": [],
            "invalid": [],
        },
    )
    assert isinstance(parse_save_recipes_batch(raw), ToolOutputContractViolation)


def test_count_id_mismatch_fails_closed():
    # created_count must match recipe_ids length (existing invariant preserved)
    raw = encode_tool_ok(
        "batch_saved",
        {
            "created_count": 2,
            "skipped_as_duplicate": 0,
            "recipe_ids": [R1],  # only 1 id for count=2
            "created": ["Борщ", "Плов"],
            "duplicates_existing": [],
            "duplicates_in_batch": [],
            "invalid": [],
        },
    )
    parsed = parse_save_recipes_batch(raw)
    assert isinstance(parsed, ToolOutputContractViolation)
