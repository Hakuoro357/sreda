"""#122 — выбор по имени из списка: title_match у читающих инструментов.

Прод-инцидент 2026-06-10 15:20 (семейный тенант): «удали масло сливочное,
одна пачка» при двух похожих пунктах → планировщик ВЫДУМАЛ несуществующий
filter()-селектор → ход прерван. Грамматика ссылок — точечные идентификаторы,
втаскивать предикаты с кавычками — хирургия + класс инъекций. Решение:
фильтр — обычный аргумент читающего инструмента (как query у search_recipes):
``list_shopping(title_match='масло сливочное')`` → дальше штатный ``.only``.

Чеклист #122 (red-before-impl):
- list_shopping/list_tasks/list_reminders принимают title_match и фильтруют
  без учёта регистра и ё; пусто после фильтра → штатный «пустой» провод;
- модели аргументов планировщика расширены (extra=forbid сохранён);
- планировщику видно поле (args-hint) и есть обучающий пример
  «удали X по имени» (валидный план);
- неоднозначность после фильтра решает существующий .only (уточнение
  из УЖЕ отфильтрованных) — без новых механизмов.
"""

from __future__ import annotations

import pytest

pymorphy3 = pytest.importorskip("pymorphy3")


class _StubEmbed:
    dim = 8

    def embed_document(self, text):
        return [1.0] + [0.0] * 7

    def embed_query(self, text):
        return [1.0] + [0.0] * 7


@pytest.fixture()
def tools(db_session):
    from sreda.services.housewife_chat_tools import build_housewife_tools

    built = build_housewife_tools(
        session=db_session, tenant_id="t1", user_id="u1",
        pending_buttons_state={}, menu_display_state={},
        embedding_client=_StubEmbed(), bot_key="sreda",
    )
    return {t.name: t for t in built}


# --- покупки -----------------------------------------------------------------


def test_list_shopping_title_match_filters(tools):
    tools["add_shopping_items"].invoke({"items": [
        {"title": "масло сливочное", "quantity_text": "1 пачка"},
        {"title": "Сливочное масло", "quantity_text": "200 г"},
        {"title": "молоко"},
    ]})
    out = tools["list_shopping"].invoke({"title_match": "масло сливочное"})
    assert "масло сливочное" in out
    assert "молоко" not in out

    # регистр и порядок слов НЕ нормализуем по словам — подстрока:
    # «МАСЛО СЛИВОЧНОЕ» (регистр) обязан найтись
    out2 = tools["list_shopping"].invoke({"title_match": "МАСЛО СЛИВОЧНОЕ"})
    assert "масло сливочное" in out2

    # широкий фильтр оставляет ОБА масла (для .only это даст честное
    # уточнение из двух) и прячет молоко
    out3 = tools["list_shopping"].invoke({"title_match": "масло"})
    assert out3.count("[sh_") == 2 and "молоко" not in out3


def test_list_shopping_title_match_yo_normalized(tools):
    tools["add_shopping_items"].invoke({"items": [{"title": "зелёный чай"}]})
    out = tools["list_shopping"].invoke({"title_match": "зеленый"})
    assert "зелёный чай" in out


def test_list_shopping_title_match_no_hits_is_empty_wire(tools):
    tools["add_shopping_items"].invoke({"items": [{"title": "молоко"}]})
    out = tools["list_shopping"].invoke({"title_match": "ананас"})
    assert out == "no shopping items"  # штатная «пустая» ветка планировщика


def test_list_shopping_without_match_unchanged(tools):
    tools["add_shopping_items"].invoke({"items": [{"title": "молоко"}]})
    out = tools["list_shopping"].invoke({})
    assert "молоко" in out and out.startswith("pending shopping items:")


# --- задачи -------------------------------------------------------------------


def test_list_tasks_title_match(tools):
    tools["add_task"].invoke({"title": "Позвонить сантехнику",
                              "scheduled_date": "tomorrow"})
    tools["add_task"].invoke({"title": "Испечь бисквиты",
                              "scheduled_date": "tomorrow"})
    out = tools["list_tasks"].invoke({"date": "all", "title_match": "сантехник"})
    assert "сантехнику" in out.lower()
    assert "бисквиты" not in out.lower()


# --- модели аргументов планировщика -------------------------------------------


def test_input_models_accept_title_match():
    from sreda.services.tool_schemas.specs_reminders import ListRemindersInput
    from sreda.services.tool_schemas.specs_shopping import ListShoppingInput
    from sreda.services.tool_schemas.specs_tasks import ListTasksInput

    assert ListShoppingInput(title_match="масло").title_match == "масло"
    assert ListRemindersInput(title_match="молоко").title_match == "молоко"
    assert ListTasksInput(title_match="сантехник").title_match == "сантехник"
    # без аргументов — по-прежнему валидно (обратная совместимость планов)
    ListShoppingInput(); ListRemindersInput(); ListTasksInput()
    with pytest.raises(Exception):
        ListShoppingInput(unknown_arg="x")  # extra=forbid сохранён


# --- видимость планировщику ----------------------------------------------------


def test_prompt_args_hint_shows_title_match():
    from sreda.runtime.planner.prompt_builder import render_tool_spec_for_prompt
    from sreda.services.tool_schemas.specs_shopping import LIST_SHOPPING_SPEC
    from sreda.services.tool_schemas.specs_tasks import LIST_TASKS_SPEC

    shop = render_tool_spec_for_prompt(LIST_SHOPPING_SPEC)
    tasks = render_tool_spec_for_prompt(LIST_TASKS_SPEC)
    assert "title_match" in shop and "title_match" in tasks
    # Codex #122 R1 MAJOR: планировщик видит ТОЛЬКО description спека (не
    # докстринги) — поведенческая подсказка обязана доезжать до промпта
    assert 'date="all"' in tasks, "нет связки date=all + title_match для задач"
    assert ".only" in shop and "filter()" in shop  # рецепт + запрет
    # Codex #122 R2 medium MINOR: у напоминаний — тот же рецепт и запрет
    from sreda.services.tool_schemas.specs_reminders import LIST_REMINDERS_SPEC

    rem = render_tool_spec_for_prompt(LIST_REMINDERS_SPEC)
    assert "title_match" in rem and ".only" in rem and "filter()" in rem


def test_few_shot_teaches_delete_by_name():
    """Обучающий пример «удали X по имени» существует и проходит полную
    валидацию (снимковая сюита прогоняет все примеры — здесь пин-проверка,
    что пример реально учит связке title_match + .only)."""
    from sreda.runtime.planner.few_shot_examples import all_examples

    blob = "\n".join(
        str(getattr(e, "plan", "")) + str(getattr(e, "user_message", ""))
        for e in all_examples()
    )
    assert "title_match" in blob, "нет примера с title_match"
    # бюджетный вариант: связка title_match+.only живёт в .only-примере,
    # запрет filter()/where() — в анти-блоке «так НЕ делай»; оба доезжают
    # до отрендеренного промпта
    from sreda.runtime.planner.few_shot_examples import render_few_shot_block

    rendered = render_few_shot_block(
        effective_llm_keys=frozenset({"humanize_result"})
    )
    assert "filter(" in rendered and "title_match" in rendered
