# -*- coding: utf-8 -*-
"""#267 Фаза A — trap: различаем «домен вне запроса» от «семья не загружена».

Раньше (trap, инцидент «удали разминку»): планировщик звал search_recipes (recipes вне allowed=tasks)
→ unavailable советовал «позови need_family» → need_family(recipes) врал «загружена ok» → доменный
фильтр резал recipes заново → петля до лимита проходов. A: `_tool_unavailable_reason` — единый
источник причины; need_family(domain_blocked) НЕ грузит и честно отвечает; unavailable не советует
need_family на домен-блок.
"""
from __future__ import annotations

from sreda.runtime.react_loop import _tool_unavailable_reason as reason


def test_tool_unavailable_reason_need_family_267():
    # семья-раздел вне allowed → domain_blocked (НЕ грузить, не врать «загружена»)
    assert reason("need_family", {"family": "recipes"}, ["tasks"], ["tasks"]) == "domain_blocked"
    # семья в allowed → available (грузить можно)
    assert reason("need_family", {"family": "shopping"}, ["shopping"], ["shopping"]) == "available"
    # семья в allowed_read но не write — частично доступна → available (read-инструменты пройдут)
    assert reason("need_family", {"family": "recipes"}, ["recipes"], ["tasks"]) == "available"
    # мусор/галлюцинация семьи → unknown_family
    assert reason("need_family", {"family": "utility"}, ["tasks"], ["tasks"]) == "unknown_family"
    assert reason("need_family", {"family": 123}, ["tasks"], ["tasks"]) == "unknown_family"
    # legacy (allowed=None) → available (домен не фильтруется)
    assert reason("need_family", {"family": "recipes"}, None, None) == "available"


def test_tool_unavailable_reason_tool_267():
    # инструмент вне раздела → domain_blocked (need_family не поможет)
    assert reason("search_recipes", {}, ["tasks"], ["tasks"]) == "domain_blocked"
    # инструмент В разделе, но семья не загружена → family_not_loaded (нужен need_family)
    assert reason("cancel_task", {}, ["tasks"], ["tasks"]) == "family_not_loaded"
    # галлюцинированный/неизвестный инструмент → unknown_tool, НЕ KeyError
    assert reason("halluc_tool_xyz", {}, ["tasks"], ["tasks"]) == "unknown_tool"
    # alias канонизируется (link_task → link_task_to_checklist), не KeyError
    assert reason("link_task", {}, ["tasks"], ["tasks"]) in (
        "domain_blocked", "family_not_loaded")  # зависит от раздела link_task_to_checklist
    # legacy (allowed=None) → family_not_loaded (старое «позови need_family»)
    assert reason("search_recipes", {}, None, None) == "family_not_loaded"


def test_tool_unavailable_reason_write_gate_267():
    # write-инструмент вне write-домена → domain_blocked (общий write-gate, чеклист п.14)
    # remove_shopping_items: read+write домен = shopping; при allowed_read=shopping,allowed_write=tasks
    # запись зарезана → domain_blocked.
    assert reason("remove_shopping_items", {}, ["shopping"], ["tasks"]) == "domain_blocked"
    # тот же при allowed write=shopping → проходит (домен ок) → family_not_loaded (нужна загрузка)
    assert reason("remove_shopping_items", {}, ["shopping"], ["shopping"]) == "family_not_loaded"
