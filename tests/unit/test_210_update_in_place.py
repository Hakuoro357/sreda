# -*- coding: utf-8 -*-
"""#210 — правка пунктов чек-листов и рецептов НА МЕСТЕ (update), а не
delete+create.

Баг (трейс 22.06, тенант Бориса): «измени пункт» в списке «Кино к просмотру»
→ планировщик вынужденно delete_checklist_item (а у удаления — confirm) +
add_checklist_items, т.к. у чек-листов/рецептов НЕ было «изменить на месте».
Эти тесты фиксируют новый контракт:
  * update_checklist_item / update_recipe меняют запись НА МЕСТЕ (тот же id),
    БЕЗ confirm; delete для правки больше не нужен.
  * ownership/active-инварианты те же, что у delete/mark (чужой/архивный →
    not_found / None, мутации НЕТ — изоляция семей).
  * новые инструменты ЗАРЕГИСТРИРОВАНЫ (manifest + короткое описание) → видны
    Фредди, и НЕ в confirm-наборе (правка не должна спрашивать удаление).

RED-before-impl для машинно-проверяемого ядра #210.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# EncryptedString требует ключ (как в test_143_checklist_by_id.py).
os.environ.setdefault(
    "SREDA_ENCRYPTION_KEY",
    "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
)

from sreda.db.base import Base  # noqa: E402
from sreda.db.models.checklists import Checklist, ChecklistItem  # noqa: E402
from sreda.db.models.core import Tenant, User  # noqa: E402
from sreda.db.models.housewife_food import Recipe  # noqa: E402
from sreda.services.checklists import ChecklistService  # noqa: E402
from sreda.services.housewife_chat_tools import build_housewife_tools  # noqa: E402
from sreda.services.housewife_recipes import HousewifeRecipeService  # noqa: E402


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    # «Своя» семья u1@t1 + «чужая» семья u2@t2 (для ownership-теста).
    s.add(Tenant(id="t1", name="T1"))
    s.add(User(id="u1", tenant_id="t1", telegram_account_id="1"))
    s.add(Tenant(id="t2", name="T2"))
    s.add(User(id="u2", tenant_id="t2", telegram_account_id="2"))
    s.commit()
    yield s
    s.close()


@pytest.fixture
def tools(session):
    return {
        t.name: t
        for t in build_housewife_tools(
            session=session, tenant_id="t1", user_id="u1",
        )
    }


# ---------------------------------------------------------------------------
# чек-листы: update_checklist_item — правка на месте + инварианты
# ---------------------------------------------------------------------------


def test_update_checklist_item_renames_in_place(tools, session):
    """Правка пункта: тот же item_id, новый текст, позиция/статус целы,
    НОВЫЙ пункт не создаётся (это и есть «не delete+add»)."""
    tools["add_checklist_items"].invoke(
        {"list_id_or_title": "Кино к просмотру",
         "items": ["Скорпион", "Машина войны"]}
    )
    svc = ChecklistService(session)
    list_id = svc.list_active(tenant_id="t1", user_id="u1")[0].id
    item = [i for i in svc.list_items(list_id=list_id) if i.title == "Скорпион"][0]
    item_id, pos = item.id, item.position

    r = tools["update_checklist_item"].invoke(
        {"item_id": item_id, "title": "Дюна (2024)"}
    )
    assert r.startswith(f"ok:updated:{item_id}:"), r
    assert "Дюна (2024)" in r

    session.expire_all()
    refreshed = session.get(ChecklistItem, item_id)
    assert refreshed is not None, "пункт исчез — правка прошла через удаление"
    assert refreshed.title == "Дюна (2024)"
    assert refreshed.position == pos        # позиция сохранена
    assert refreshed.status == "pending"    # статус сохранён
    # всего по-прежнему 2 пункта — правка НЕ создала третий
    assert len(svc.list_items(list_id=list_id)) == 2


def test_update_checklist_item_preserves_done_status(tools, session):
    """Правка done-пункта не сбрасывает статус (меняется только текст)."""
    tools["add_checklist_items"].invoke(
        {"list_id_or_title": "Дела", "items": ["X"]}
    )
    svc = ChecklistService(session)
    list_id = svc.list_active(tenant_id="t1", user_id="u1")[0].id
    item = svc.list_items(list_id=list_id)[0]
    svc.mark_done(item_id=item.id)

    r = tools["update_checklist_item"].invoke({"item_id": item.id, "title": "Y"})
    assert r.startswith("ok:updated:"), r
    session.expire_all()
    refreshed = session.get(ChecklistItem, item.id)
    assert refreshed.title == "Y"
    assert refreshed.status == "done"       # done не сбросился


def test_update_checklist_item_empty_title_is_title_required(tools, session):
    """Пустой новый текст → title_required; пункт НЕ тронут."""
    tools["add_checklist_items"].invoke(
        {"list_id_or_title": "Дела", "items": ["X"]}
    )
    svc = ChecklistService(session)
    item = svc.list_items(
        list_id=svc.list_active(tenant_id="t1", user_id="u1")[0].id
    )[0]
    r = tools["update_checklist_item"].invoke({"item_id": item.id, "title": "   "})
    assert r == "error: title_required", r
    session.expire_all()
    assert session.get(ChecklistItem, item.id).title == "X"


def test_update_foreign_checklist_item_not_found_no_mutation(tools, session):
    """Чужой пункт (t2/u2) → item_not_found, мутации НЕТ, id не светим."""
    foreign = ChecklistService(session)
    cl = foreign.create_list(tenant_id="t2", user_id="u2", title="Чужой")
    [item], _ = foreign.add_items(list_id=cl.id, items=["Секрет"])
    fid = item.id

    r = tools["update_checklist_item"].invoke({"item_id": fid, "title": "Взлом"})
    assert r == "error: item_not_found", r
    assert fid not in r
    session.expire_all()
    assert session.get(ChecklistItem, fid).title == "Секрет", (
        "чужой пункт изменён — нарушена изоляция семей"
    )


def test_update_archived_checklist_item_not_found_no_mutation(tools, session):
    """Пункт АРХИВНОГО списка → item_not_found, без мутации (active-фильтр
    зашит в атомарный UPDATE, как у mark/delete)."""
    tools["add_checklist_items"].invoke(
        {"list_id_or_title": "Дела", "items": ["X"]}
    )
    svc = ChecklistService(session)
    cl = svc.list_active(tenant_id="t1", user_id="u1")[0]
    target_id = svc.list_items(list_id=cl.id)[0].id
    session.get(Checklist, cl.id).status = "archived"
    session.commit()

    r = tools["update_checklist_item"].invoke({"item_id": target_id, "title": "Y"})
    assert r == "error: item_not_found", r
    session.expire_all()
    assert session.get(ChecklistItem, target_id).title == "X"


# ---------------------------------------------------------------------------
# рецепты: update_recipe — правка на месте + инварианты
# ---------------------------------------------------------------------------


def test_update_recipe_edits_in_place_same_id(session):
    """Правка рецепта: тот же recipe_id, новые поля, старый НЕ удалён."""
    svc = HousewifeRecipeService(session)
    recipe, _ = svc.save_recipe(
        tenant_id="t1", user_id="u1", title="Борщ",
        ingredients=[{"title": "свёкла"}], instructions_md="Старые шаги",
        servings=4, source="user_dictated",
    )
    rid = recipe.id
    updated = svc.update_recipe(
        tenant_id="t1", user_id="u1", recipe_id=rid,
        title="Борщ украинский", instructions_md="Новые шаги", servings=6,
    )
    assert updated is not None
    assert updated.id == rid                       # тот же id, не создан новый
    assert updated.title == "Борщ украинский"
    assert updated.instructions_md == "Новые шаги"
    assert updated.servings == 6
    # старый рецепт не удалён — в книге ровно одна строка
    assert session.query(Recipe).count() == 1


def test_update_recipe_partial_keeps_untouched_fields(session):
    """None-поля НЕ трогаются (частичная правка)."""
    svc = HousewifeRecipeService(session)
    recipe, _ = svc.save_recipe(
        tenant_id="t1", user_id="u1", title="Плов",
        ingredients=[{"title": "рис"}], instructions_md="шаги", servings=4,
        source="user_dictated",
    )
    rid = recipe.id
    svc.update_recipe(
        tenant_id="t1", user_id="u1", recipe_id=rid, title="Плов с курицей",
    )
    session.expire_all()
    r = session.get(Recipe, rid)
    assert r.title == "Плов с курицей"
    assert r.servings == 4              # не передавали — не изменилось
    assert r.instructions_md == "шаги"  # не передавали — не изменилось


def test_update_recipe_replaces_ingredients(session):
    """Переданный ingredients ЗАМЕНЯЕТ весь список (cascade delete-orphan)."""
    svc = HousewifeRecipeService(session)
    recipe, _ = svc.save_recipe(
        tenant_id="t1", user_id="u1", title="Салат",
        ingredients=[{"title": "огурец"}, {"title": "помидор"}],
        source="user_dictated",
    )
    rid = recipe.id
    svc.update_recipe(
        tenant_id="t1", user_id="u1", recipe_id=rid,
        ingredients=[{"title": "капуста"}, {"title": "морковь"},
                     {"title": "масло"}],
    )
    session.expire_all()
    r = session.get(Recipe, rid)
    titles = [i.title for i in sorted(r.ingredients, key=lambda x: x.sort_order)]
    assert titles == ["капуста", "морковь", "масло"]   # старые заменены целиком


def test_update_recipe_ingredients_none_does_not_wipe(session):
    """ingredients=None (не передан) → ингредиенты НЕ трогаются (анти-стирание)."""
    svc = HousewifeRecipeService(session)
    recipe, _ = svc.save_recipe(
        tenant_id="t1", user_id="u1", title="Каша",
        ingredients=[{"title": "крупа"}, {"title": "молоко"}],
        source="user_dictated",
    )
    rid = recipe.id
    svc.update_recipe(tenant_id="t1", user_id="u1", recipe_id=rid, title="Каша вкусная")
    session.expire_all()
    r = session.get(Recipe, rid)
    assert len(r.ingredients) == 2, "ингредиенты стёрлись при правке только названия"


def test_update_foreign_recipe_returns_none_no_mutation(session):
    """Чужой рецепт (t2/u2) → None, мутации НЕТ."""
    svc = HousewifeRecipeService(session)
    recipe, _ = svc.save_recipe(
        tenant_id="t2", user_id="u2", title="Чужой рецепт",
        ingredients=[{"title": "X"}], source="user_dictated",
    )
    rid = recipe.id
    out = svc.update_recipe(
        tenant_id="t1", user_id="u1", recipe_id=rid, title="Взлом",
    )
    assert out is None
    session.expire_all()
    assert session.get(Recipe, rid).title == "Чужой рецепт", (
        "чужой рецепт изменён — нарушена изоляция семей"
    )


def test_update_recipe_tool_ok(tools, session):
    """Tool-слой: update_recipe → ok:updated:<id>, правка применилась."""
    svc = HousewifeRecipeService(session)
    recipe, _ = svc.save_recipe(
        tenant_id="t1", user_id="u1", title="Каша",
        ingredients=[{"title": "крупа"}], source="user_dictated",
    )
    rid = recipe.id
    r = tools["update_recipe"].invoke({"recipe_id": rid, "title": "Каша гречневая"})
    assert r == f"ok:updated:{rid}", r
    session.expire_all()
    assert session.get(Recipe, rid).title == "Каша гречневая"


# ---------------------------------------------------------------------------
# регистрация: новые инструменты видны Фредди и НЕ требуют confirm
# ---------------------------------------------------------------------------


def test_new_update_tools_registered_and_no_confirm():
    """Без манифест-записи фильтр ReAct (TOOL_FAMILY_MANIFEST) выкинул бы
    инструмент; в confirm-наборе — спрашивал бы удаление на правке."""
    from sreda.runtime.react_loop import (
        _CONFIRM_PHRASE,
        _EXTRA_FAMILIES,
        _REACT_TOOL_DESC,
    )
    from sreda.services.tool_schemas.families import TOOL_FAMILY_MANIFEST

    for name, fam in [
        ("update_checklist_item", "checklists"),
        ("update_recipe", "recipes"),
    ]:
        assert TOOL_FAMILY_MANIFEST.get(name) == fam, (
            f"{name} нет в манифесте / не та семья"
        )
        assert fam in _EXTRA_FAMILIES, f"семья {fam} не экспонирована в ReAct"
        assert name in _REACT_TOOL_DESC, (
            f"{name} без короткого описания для Фредди"
        )
        assert name not in _CONFIRM_PHRASE, (
            f"{name} ошибочно в confirm-наборе — правка не должна спрашивать удаление"
        )


def test_update_tools_exposed_in_housewife_toolset(tools):
    """Инструменты реально собираются build_housewife_tools (оба пути)."""
    assert "update_checklist_item" in tools
    assert "update_recipe" in tools


# ---------------------------------------------------------------------------
# R1-фиксы ревью (subagent CRITICAL + Codex MAJOR/MINOR)
# ---------------------------------------------------------------------------


def test_update_recipe_rename_recomputes_dedup_hash_ctx(session):
    """🔴 subagent R1 CRITICAL: после переименования рецепта сохранение НОВОГО
    рецепта со СТАРЫМ названием на ctx-пути создаёт отдельную строку (хеш
    дедупа следует за живым названием), а не возвращает переименованный как
    existing → не теряем новый рецепт."""
    from sreda.runtime.planner.tool_runtime import (
        ToolRuntimeContext,
        bind_tool_runtime,
    )

    def _ctx(exec_id: str) -> ToolRuntimeContext:
        return ToolRuntimeContext(
            operation_id=f"op-{exec_id}", execution_id=exec_id, step_id="s1",
            tool_name="save_recipe", tenant_id="t1", user_id="u1", turn_key="tk1",
        )

    svc = HousewifeRecipeService(session)
    # 1) создаём «Борщ» на ctx-пути → получает hash(«Борщ»)
    with bind_tool_runtime(_ctx("e1")):
        r1, new1 = svc.save_recipe(
            tenant_id="t1", user_id="u1", title="Борщ",
            ingredients=[{"title": "свёкла"}],
        )
    assert new1 is True
    rid = r1.id
    # 2) переименовываем «Борщ» → «Окрошка» (хеш ДОЛЖЕН пересчитаться)
    svc.update_recipe(tenant_id="t1", user_id="u1", recipe_id=rid, title="Окрошка")
    # 3) сохраняем НОВЫЙ «Борщ» на ctx-пути — НЕ должен матчить переименованный
    with bind_tool_runtime(_ctx("e2")):
        r2, new2 = svc.save_recipe(
            tenant_id="t1", user_id="u1", title="Борщ",
            ingredients=[{"title": "картошка"}],
        )
    assert new2 is True, "новый «Борщ» дропнулся — устаревший хеш переименованного матчит"
    assert r2.id != rid
    assert session.query(Recipe).count() == 2


def test_update_recipe_empty_ingredients_wipes_all(session):
    """мимо R1 MINOR: явный пустой список ингредиентов очищает все строки
    (легально — instructions-only рецепт)."""
    svc = HousewifeRecipeService(session)
    recipe, _ = svc.save_recipe(
        tenant_id="t1", user_id="u1", title="Тост",
        ingredients=[{"title": "хлеб"}, {"title": "масло"}],
        source="user_dictated",
    )
    rid = recipe.id
    svc.update_recipe(tenant_id="t1", user_id="u1", recipe_id=rid, ingredients=[])
    session.expire_all()
    assert len(session.get(Recipe, rid).ingredients) == 0


def test_update_recipe_invalid_servings_is_graceful_no_partial(session):
    """Codex high R1 MAJOR: разбор ДО мутации — невалидные порции не валят
    правку и не оставляют частичную мутацию (title применяется, servings нет)."""
    svc = HousewifeRecipeService(session)
    recipe, _ = svc.save_recipe(
        tenant_id="t1", user_id="u1", title="Суп",
        ingredients=[{"title": "вода"}], servings=4, source="user_dictated",
    )
    rid = recipe.id
    out = svc.update_recipe(
        tenant_id="t1", user_id="u1", recipe_id=rid,
        title="Суп гороховый", servings="не число",  # type: ignore[arg-type]
    )
    assert out is not None
    session.expire_all()
    r = session.get(Recipe, rid)
    assert r.title == "Суп гороховый"   # валидное поле применилось
    assert r.servings == 4               # невалидные порции — без изменений


def test_update_recipe_invalid_cooking_time_does_not_clobber(session):
    """Codex high R2 MAJOR: вне-диапазонное/невалидное время НЕ затирает
    существующее cooking_time_minutes (как servings); валидное — применяется."""
    svc = HousewifeRecipeService(session)
    recipe, _ = svc.save_recipe(
        tenant_id="t1", user_id="u1", title="Рагу",
        ingredients=[{"title": "овощи"}], cooking_time_minutes=30,
        source="user_dictated",
    )
    rid = recipe.id
    # вне диапазона (999 > 600) → НЕ трогаем
    svc.update_recipe(
        tenant_id="t1", user_id="u1", recipe_id=rid, cooking_time_minutes=999,
    )
    session.expire_all()
    assert session.get(Recipe, rid).cooking_time_minutes == 30, "невалидное время затёрло существующее"
    # валидное → применяется
    svc.update_recipe(
        tenant_id="t1", user_id="u1", recipe_id=rid, cooking_time_minutes=45,
    )
    session.expire_all()
    assert session.get(Recipe, rid).cooking_time_minutes == 45


def test_update_recipe_tool_noop_and_blank_title(tools, session):
    """Codex R1 MINOR: вызов без полей → no_fields_to_update; пустой title →
    title_required (не рапортовать «обновила», когда менять нечего)."""
    svc = HousewifeRecipeService(session)
    recipe, _ = svc.save_recipe(
        tenant_id="t1", user_id="u1", title="Каша",
        ingredients=[{"title": "крупа"}], source="user_dictated",
    )
    rid = recipe.id
    assert tools["update_recipe"].invoke({"recipe_id": rid}) == "error: no_fields_to_update"
    assert tools["update_recipe"].invoke(
        {"recipe_id": rid, "title": "   "}
    ) == "error: title_required"


def test_update_tools_registered_in_unbacked_claim_maps():
    """Codex medium R1 MAJOR #2: update_* в категориях detect_unbacked_claim +
    в картах handlers — иначе «обновила рецепт»/«изменила пункт» после реального
    вызова сочлись бы галлюцинацией / не зачистились бы на легаси-чате."""
    from sreda.services.llm import _CATEGORY_TO_TOOLS, _KNOWN_TOOL_NAMES
    from sreda.runtime.handlers import _TOOL_NAMES_SET, _TOOL_TO_DOMAIN
    assert "update_recipe" in _CATEGORY_TO_TOOLS["recipe"]
    assert "update_checklist_item" in _CATEGORY_TO_TOOLS["checklist"]
    assert "update_recipe" in _KNOWN_TOOL_NAMES  # зачистка leaked-синтаксиса
    assert "update_recipe" in _TOOL_NAMES_SET
    assert "update_checklist_item" in _TOOL_NAMES_SET
    assert _TOOL_TO_DOMAIN["update_recipe"] == "рецепты"
    assert _TOOL_TO_DOMAIN["update_checklist_item"] == "чек-лист"


def test_update_recipe_claim_is_backed_by_tool():
    """Функционально: «обновила рецепт» с called_tools={update_recipe} НЕ
    флагается необоснованным (а без инструмента — флагается)."""
    from sreda.services.llm import detect_unbacked_claim
    claim = "Обновила рецепт борща."
    assert detect_unbacked_claim(claim, set()) is True
    assert detect_unbacked_claim(claim, {"update_recipe"}) is False
