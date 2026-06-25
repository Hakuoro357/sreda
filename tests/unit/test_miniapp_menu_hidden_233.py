# -*- coding: utf-8 -*-
"""#233 — «Расписание» (id="schedule") скрыт из home-меню Mini App,
остальные секции и порядок не затронуты, бэкенд цел."""
from __future__ import annotations

from sreda.api.routes.miniapp import _HIDDEN_MENU_SECTION_IDS, _menu_items_for_render
from sreda.features.contracts import MiniAppSection


def _sec(section_id: str, title: str) -> MiniAppSection:
    return MiniAppSection(
        id=section_id, title=title, icon="x",
        route=f"#/{section_id}", subtitle=None, count=0,
    )


def test_schedule_section_hidden_others_intact() -> None:
    sections = [
        _sec("reminders", "Напоминания"),
        _sec("schedule", "Расписание"),
        _sec("checklists", "Дела"),
        _sec("shopping", "Покупки"),
    ]
    ids = [i["id"] for i in _menu_items_for_render(sections)]
    assert "schedule" not in ids                                   # скрыт
    assert ids == ["reminders", "checklists", "shopping"]          # порядок + остальные целы


def test_hidden_set_targets_only_schedule() -> None:
    # точечный фильтр: только schedule, чтобы не зацепить соседей
    assert _HIDDEN_MENU_SECTION_IDS == frozenset({"schedule"})


def test_non_hidden_section_mapped_with_all_fields() -> None:
    items = _menu_items_for_render([_sec("recipes", "Рецепты")])
    assert items == [{
        "id": "recipes", "title": "Рецепты", "icon": "x",
        "route": "#/recipes", "subtitle": None, "count": 0,
    }]
