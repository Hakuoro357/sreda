"""#115 — checklist "готово" trio surfaces the NAME to the live voice.

create_checklist / delete_checklist_item / mark_checklist_item_done already parse a
`title` into their typed output; #115 only adds display_summary (@computed_field) +
__display_field__ so the presenter shows the name (no okv2/service/parser change).
"""

from __future__ import annotations

import pytest

from sreda.services.composer.presenters import (
    build_display_field_map,
    render_display_text,
    set_display_field_map,
)
from sreda.services.tool_schemas.housewife import parse_tool_output
from sreda.services.tool_schemas.specs import ALL_TOOL_SPECS

CL = "checklist_" + "a" * 24
IT = "clitem_" + "a" * 24


@pytest.fixture(autouse=True)
def _install_display_map():
    set_display_field_map(build_display_field_map(ALL_TOOL_SPECS))
    yield
    set_display_field_map({})


@pytest.mark.parametrize(
    "tool,raw,status,name",
    [
        ("create_checklist", f"ok:created:{CL}:Планы на крой", "created", "Планы на крой"),
        ("mark_checklist_item_done", f"ok:done:{IT}:Купить ткань", "done", "Купить ткань"),
        ("delete_checklist_item", f"ok:deleted:{IT}:Старый пункт", "deleted", "Старый пункт"),
    ],
)
def test_display_shows_title_not_id(tool, raw, status, name):
    parsed = parse_tool_output(tool, raw)
    dumped = parsed.model_dump()
    assert "display_summary" in dumped  # computed field reaches the presenter dict
    text = render_display_text(tool, dumped, domain_status=status)
    assert name in text
    assert CL not in text and IT not in text  # raw ids never leak


def test_registry_maps_display_field_for_trio():
    # build_display_field_map(ALL_TOOL_SPECS) registers (tool,status)->display_summary
    m = build_display_field_map(ALL_TOOL_SPECS)
    assert m[("create_checklist", "created")] == "display_summary"
    assert m[("mark_checklist_item_done", "done")] == "display_summary"
    assert m[("delete_checklist_item", "deleted")] == "display_summary"
