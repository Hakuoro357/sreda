"""#115 — shopping bulk/update tools return affected NAMES (red-before-impl).

mark_shopping_bought / remove_shopping_items / update_shopping_items_category:
by-name outcome (affected names, not_eligible names, not_found_count).
update_shopping_item: previous_name / new_name. Tested at the wire→parser→model→
presenter surface; the okv2 wire strings are exactly what the chat-tools emit.
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
    MarkShoppingBoughtOk,
    RemoveShoppingItemsOk,
    UpdateShoppingItemOk,
    UpdateShoppingItemsCategoryOk,
    parse_mark_shopping_bought,
    parse_remove_shopping_items,
    parse_update_shopping_item,
    parse_update_shopping_items_category,
)
from sreda.services.tool_schemas.specs import ALL_TOOL_SPECS
from sreda.services.tool_schemas.tool_ok_codec import encode_tool_ok

SH = "sh_" + "a" * 24


@pytest.fixture(autouse=True)
def _install_display_map():
    set_display_field_map(build_display_field_map(ALL_TOOL_SPECS))
    yield
    set_display_field_map({})


# --- mark_shopping_bought ---------------------------------------------------


def test_mark_okv2_names_and_failures():
    raw = encode_tool_ok(
        "bought",
        {"bought_count": 2, "marked": ["молоко", "хлеб"],
         "not_eligible": ["сыр"], "not_found_count": 1},
    )
    parsed = parse_mark_shopping_bought(raw)
    assert isinstance(parsed, MarkShoppingBoughtOk)
    assert parsed.marked == ["молоко", "хлеб"]
    assert parsed.not_eligible == ["сыр"]
    s = parsed.display_summary
    assert "молоко" in s and "хлеб" in s and "сыр" in s and "1 не нашла" in s


def test_mark_presenter_shows_names_hides_ids():
    raw = encode_tool_ok(
        "bought",
        {"bought_count": 1, "marked": ["молоко"], "not_eligible": [], "not_found_count": 0},
    )
    parsed = parse_mark_shopping_bought(raw)
    text = render_display_text("mark_shopping_bought", parsed.model_dump(), domain_status="bought")
    assert "молоко" in text and SH not in text


def test_mark_legacy_positional_still_parses():
    parsed = parse_mark_shopping_bought("ok:bought:3")
    assert isinstance(parsed, MarkShoppingBoughtOk)
    assert parsed.bought_count == 3 and parsed.marked == []
    assert parsed.display_summary == "Готово."


def test_mark_count_name_mismatch_and_blank_fail_closed():
    bad1 = encode_tool_ok("bought", {"bought_count": 2, "marked": ["молоко"],
                                     "not_eligible": [], "not_found_count": 0})
    bad2 = encode_tool_ok("bought", {"bought_count": 1, "marked": ["  "],
                                     "not_eligible": [], "not_found_count": 0})
    assert isinstance(parse_mark_shopping_bought(bad1), ToolOutputContractViolation)
    assert isinstance(parse_mark_shopping_bought(bad2), ToolOutputContractViolation)
    assert isinstance(parse_mark_shopping_bought("okv2:bought:{bad"), ToolOutputContractViolation)


# --- remove_shopping_items ---------------------------------------------------


def test_remove_okv2_names():
    raw = encode_tool_ok(
        "removed",
        {"removed_count": 1, "removed": ["молоко"], "not_eligible": ["хлеб"],
         "not_found_count": 2},
    )
    parsed = parse_remove_shopping_items(raw)
    assert isinstance(parsed, RemoveShoppingItemsOk)
    assert parsed.removed == ["молоко"]
    s = parsed.display_summary
    assert "Убрала" in s and "молоко" in s and "хлеб" in s and "2 не нашла" in s


def test_remove_legacy_and_failures():
    parsed = parse_remove_shopping_items("ok:removed:2")
    assert isinstance(parsed, RemoveShoppingItemsOk)
    assert parsed.removed_count == 2 and parsed.display_summary == "Готово."
    bad = encode_tool_ok("removed", {"removed_count": 2, "removed": ["x"],
                                     "not_eligible": [], "not_found_count": 0})
    assert isinstance(parse_remove_shopping_items(bad), ToolOutputContractViolation)


# --- update_shopping_item ----------------------------------------------------


def test_update_item_rename_shows_before_after():
    raw = encode_tool_ok("updated", {"item_id": SH, "previous_name": "молоко",
                                     "new_name": "кефир"})
    parsed = parse_update_shopping_item(raw)
    assert isinstance(parsed, UpdateShoppingItemOk)
    s = parsed.display_summary
    assert "Переименовала" in s and "молоко" in s and "кефир" in s and SH not in s


def test_update_item_same_name_shows_updated():
    raw = encode_tool_ok("updated", {"item_id": SH, "previous_name": "молоко",
                                     "new_name": "молоко"})
    parsed = parse_update_shopping_item(raw)
    assert "Обновила" in parsed.display_summary and "молоко" in parsed.display_summary


def test_update_item_legacy_and_failures():
    parsed = parse_update_shopping_item(f"ok:updated:{SH}")
    assert isinstance(parsed, UpdateShoppingItemOk)
    assert parsed.new_name is None and parsed.display_summary == "Готово."
    # okv2 must carry usable names
    for payload in (
        {"item_id": SH, "previous_name": "молоко"},               # missing new_name
        {"item_id": SH, "previous_name": " ", "new_name": "x"},   # blank previous
    ):
        assert isinstance(
            parse_update_shopping_item(encode_tool_ok("updated", payload)),
            ToolOutputContractViolation,
        ), payload


# --- update_shopping_items_category -------------------------------------------


def test_category_okv2_names_and_category_label():
    raw = encode_tool_ok(
        "updated_category",
        {"updated_count": 2, "updated": ["яйца", "сыр"], "not_eligible": [],
         "category": "молочные", "not_found_count": 1},
    )
    parsed = parse_update_shopping_items_category(raw)
    assert isinstance(parsed, UpdateShoppingItemsCategoryOk)
    s = parsed.display_summary
    assert "молочные" in s and "яйца" in s and "сыр" in s and "1 не нашла" in s


def test_category_legacy_and_failures():
    parsed = parse_update_shopping_items_category("ok:updated:5")
    assert isinstance(parsed, UpdateShoppingItemsCategoryOk)
    assert parsed.updated_count == 5 and parsed.display_summary == "Готово."
    bad = encode_tool_ok(
        "updated_category",
        {"updated_count": 1, "updated": ["a", "b"], "not_eligible": [],
         "category": "x", "not_found_count": 0},
    )
    assert isinstance(parse_update_shopping_items_category(bad), ToolOutputContractViolation)


def test_category_missing_or_unusable_fails_closed():
    # Codex #115 shop4 [MAJOR]: okv2 must carry a usable target category.
    for payload in (
        {"updated_count": 1, "updated": ["яйца"], "not_eligible": [], "not_found_count": 0},  # missing
        {"updated_count": 1, "updated": ["яйца"], "not_eligible": [], "category": "«»", "not_found_count": 0},  # sanitizes empty
    ):
        assert isinstance(
            parse_update_shopping_items_category(encode_tool_ok("updated_category", payload)),
            ToolOutputContractViolation,
        ), payload


def test_service_blank_id_counts_as_not_found(db_session):
    # Codex #115 shop4 [MAJOR]: a blank requested id must surface as not_found,
    # not silently vanish in dedup. Service-level (skips if env lacks pymorphy3).
    pytest.importorskip("pymorphy3")
    from sreda.services.housewife_shopping import HousewifeShoppingService

    svc = HousewifeShoppingService(db_session)
    rows = svc.add_items(
        tenant_id="t1", user_id="u1", items=[{"title": "молоко"}]
    )
    res = svc.mark_bought_detailed(
        tenant_id="t1", user_id="u1", ids=[rows[0].id, "", "sh_" + "f" * 24]
    )
    assert [r.title for r in res.affected] == ["молоко"]
    assert res.not_found_count == 2  # blank + unknown both counted


def test_registry_maps_display_field_for_all_four():
    m = build_display_field_map(ALL_TOOL_SPECS)
    assert m[("mark_shopping_bought", "bought")] == "display_summary"
    assert m[("remove_shopping_items", "removed")] == "display_summary"
    assert m[("update_shopping_item", "updated")] == "display_summary"
    assert m[("update_shopping_items_category", "updated_category")] == "display_summary"
