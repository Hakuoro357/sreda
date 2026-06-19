"""#165 Этап 1: обрезка описаний инструментов ТОЛЬКО для Фредди (react).

Фиксирует контракт: семейные инструменты получают короткие описания; критичная
семантика (enum/дедуп/id-vs-name/разрушающее) сохранена; WRITE_GUARD перенесён в
системный промпт; легаси-чат (build_housewife_tools напрямую) НЕ тронут; инструменты
по-прежнему вызываются (model_copy сохранил func/schema).
"""
from __future__ import annotations

from sreda.runtime.react_loop import (
    _REACT_TOOL_DESC,
    _system_prompt,
    build_slice_tools,
)
from sreda.services.housewife_chat_tools import build_housewife_tools
from tests.unit.conftest import seed_telegram_user


def _slice(db_session):
    u = seed_telegram_user(db_session); db_session.commit()
    return u, {t.name: t for t in build_slice_tools(db_session, u.tenant_id, u.user_id)}


def test_family_tools_get_short_descriptions(db_session):
    _, tools = _slice(db_session)
    # семейные инструменты получили КОРОТКОЕ описание из реестра
    for name in ("save_recipe", "plan_week_menu", "add_checklist_items", "get_weather"):
        assert tools[name].description == _REACT_TOOL_DESC[name], name
    # и оно заметно короче прежнего раздутого (save_recipe был ~1216 токенов описания)
    assert len(tools["save_recipe"].description) < 1200


def test_critical_enums_and_invariants_preserved(db_session):
    _, tools = _slice(db_session)
    sr = tools["save_recipe"].description
    for v in ("user_dictated", "ai_generated", "web_found", "upgraded_from_menu"):
        assert v in sr, f"source enum {v} потерян"
    assert "duplicate" in sr.lower() or "дедуп" in sr.lower() or "вариаци" in sr.lower()
    # роли семьи
    fam = tools["add_family_members"].description
    for role in ("self", "spouse", "child", "parent", "other"):
        assert role in fam, role
    # категории покупок
    assert "молочные" in tools["add_shopping_items"].description
    # granularity погоды
    gw = tools["get_weather"].description
    for g in ("daily", "part_of_day", "hourly"):
        assert g in gw, g
    # meal_type меню
    assert "breakfast" in tools["update_menu_item"].description
    # разрушающий перенос задачи помечен
    assert "РАЗРУШ" in tools["move_task_to_checklist"].description.upper()
    # коды возврата генерации покупок
    gen = tools["generate_shopping_from_menu"].description
    assert "ok:generated" in gen and "plan_not_found" in gen
    # id-vs-name у mark/delete
    assert "clitem_" in tools["mark_checklist_item_done"].description
    # недоверенный контент fetch_url
    assert "не выполняй" in tools["fetch_url"].description.lower()


def test_write_guard_relocated_to_system_prompt(db_session):
    # суть WRITE_GUARD теперь в системном промпте ОДИН раз (правило #8) — ПОЛНЫЙ порт R-30
    sp = _system_prompt("2030-01-01 (Вторник)")
    assert "записать?" in sp
    assert "prior knowledge" in sp.lower()
    assert "8." in sp  # правило №8
    # позитивная рамка (R2, Codex high MAJOR): write только при явном намерении, по смыслу
    assert "ТОЛЬКО" in sp and "ЯВНОМ" in sp
    assert "ПО СМЫСЛУ" in sp
    # ключевые write-глаголы напоминаний/задач не потеряны
    for verb in ("напомнить", "запланировать", "отменить", "перенести"):
        assert verb in sp, verb
    # и per-tool описания Фредди НЕ несут полный гард-блок (анти-дубль)
    _, tools = _slice(db_session)
    for name in ("save_recipe", "add_shopping_items", "add_checklist_items"):
        assert "Список иллюстративный" not in tools[name].description, name


def test_legacy_housewife_tools_keep_full_docstring(db_session):
    """Легаси-чат (handlers.py) строит СВОИ объекты через build_housewife_tools —
    они НЕ должны быть тронуты (model_copy в build_slice_tools не мутирует общий объект)."""
    u = seed_telegram_user(db_session); db_session.commit()
    legacy = {t.name: t for t in build_housewife_tools(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        pending_buttons_state=None, menu_display_state=None, embedding_client=None)}
    # у write-инструмента легаси-версия несёт полный WRITE_GUARD + длинный докстринг
    assert "Список иллюстративный" in legacy["save_recipe"].description
    assert len(legacy["save_recipe"].description) > len(_REACT_TOOL_DESC["save_recipe"])


def test_bespoke_and_invocation_intact(db_session):
    _, tools = _slice(db_session)
    # bespoke напоминания/задачи не в реестре обрезки (их докстринги уже короткие)
    assert "list_reminders" not in _REACT_TOOL_DESC
    assert "add_task" not in _REACT_TOOL_DESC
    # инструмент с подменённым описанием по-прежнему вызывается (func/schema целы)
    out = tools["list_shopping"].invoke({"title_match": ""})
    assert isinstance(out, str)
