"""#165 Этап 2 (Срез A) — RED-контракт ленивой загрузки семей (need_family + динамический bind).

Пинит пункт 1 чеклиста принятия (наблюдаемый исход): просьба из НЕ предзагруженной семьи →
семья добирается по need_family → её инструмент РЕАЛЬНО выполняется в том же ходу, и
active_families в state содержит семью после добора.

КРАСНЫЙ ДО реализации Среза A (механизма ещё нет: нет need_family-тула, нет active_families в
ReactState, нет per-invocation bind). Зелёным станет, когда Срез A реализован. НЕ удалять —
это контракт ядра R5-плана (plans/165-stage2-final.md §1, §8-A).

Стаб LLM ЗАПИСЫВАЕТ набор инструментов на КАЖДЫЙ bind_tools (плана §1 Phase A: текущий
test_react_loop_graph._StubLLM с `bind_tools→self` это не ловит).
"""

from __future__ import annotations

import pytest

from langchain_core.messages import AIMessage

from sreda.runtime import react_loop
from tests.unit.conftest import seed_telegram_user


class _RecordingStubLLM:
    """Скриптованный LLM, который ЗАПОМИНАЕТ набор инструментов каждого bind_tools."""

    def __init__(self, scripted: list[AIMessage]) -> None:
        self._scripted = scripted
        self._i = 0
        self.binds: list[set[str]] = []

    def bind_tools(self, tools):  # noqa: ANN001
        self.binds.append({getattr(t, "name", None) for t in tools})
        return self

    def invoke(self, messages):  # noqa: ANN001
        msg = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return msg


@pytest.mark.asyncio
async def test_need_family_loads_family_and_executes_tool(db_session):
    """Срез A контракт: need_family(shopping) → add_shopping_items доступен и выполняется."""
    u = seed_telegram_user(db_session)
    db_session.commit()

    scripted = [
        # проход 1: семья покупок НЕ предзагружена → модель просит её
        AIMessage(content="", tool_calls=[{
            "name": "need_family", "args": {"family": "shopping"}, "id": "call_nf",
        }]),
        # проход 2: семья догружена → реальный вызов покупок (items — список СЛОВАРЕЙ)
        AIMessage(content="", tool_calls=[{
            "name": "add_shopping_items",
            "args": {"items": [{"title": "молоко"}, {"title": "хлеб"}]}, "id": "call_add",
        }]),
        AIMessage(content="Готово."),
    ]
    stub = _RecordingStubLLM(scripted)

    reply = await react_loop.handle_turn(
        session=db_session,
        tenant_id=u.tenant_id,
        user_id=u.user_id,
        thread_id="lazy165-1",
        llm=stub,
        user_text="добавь молоко и хлеб в покупки",
        inbound_message_id="lazy165-1-msg",
        channel="react",
    )

    # (i) на ПЕРВОМ проходе семьи покупок в наборе НЕ было (обрезка)
    assert stub.binds, "bind_tools не вызывался"
    assert "add_shopping_items" not in stub.binds[0], (
        "Срез A: на старте семья shopping не должна быть привязана (ленивая загрузка)")
    # (ii) после need_family на ВТОРОМ проходе семья появилась
    assert len(stub.binds) >= 2 and "add_shopping_items" in stub.binds[1], (
        "Срез A: после need_family(shopping) инструмент покупок должен быть в наборе")
    # (iii) инструмент реально выполнился — позиции покупок созданы
    from sreda.db.models.housewife_food import ShoppingListItem  # модель покупок
    rows = db_session.query(ShoppingListItem).filter(
        ShoppingListItem.tenant_id == u.tenant_id).all()
    titles = {r.title for r in rows}
    assert {"молоко", "хлеб"} <= titles, f"покупки не созданы: {titles}"
    assert reply  # ход завершился ответом, не пустым fallback


@pytest.mark.asyncio
async def test_guard_loads_family_when_model_refuses(db_session):
    """Срез A пункт 2: модель сама не дозвалась («не умею») → guard догружает семью и ход
    завершается ДЕЛОМ (не молчаливым отказом)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    scripted = [
        AIMessage(content="Извини, пока не умею добавлять в покупки."),  # ОТКАЗ, без tool_call
        AIMessage(content="", tool_calls=[{  # после guard-добора — реальный вызов
            "name": "add_shopping_items",
            "args": {"items": [{"title": "молоко"}]}, "id": "call_add",
        }]),
        AIMessage(content="Готово, добавила молоко."),
    ]
    stub = _RecordingStubLLM(scripted)
    reply = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="lazy165-guard", llm=stub, user_text="добавь молоко в покупки",
        inbound_message_id="lazy165-guard-msg", channel="react")

    # guard сработал: shopping не было на 1-м проходе, появилось на 2-м
    assert "add_shopping_items" not in stub.binds[0]
    assert len(stub.binds) >= 2 and "add_shopping_items" in stub.binds[1]
    # ход завершился ДЕЛОМ — позиция создана
    from sreda.db.models.housewife_food import ShoppingListItem
    rows = db_session.query(ShoppingListItem).filter(
        ShoppingListItem.tenant_id == u.tenant_id).all()
    assert "молоко" in {r.title for r in rows}
    assert reply


@pytest.mark.asyncio
async def test_guard_does_not_fire_on_out_of_scope(db_session):
    """Срез A пункт 2 (анти-false-positive): вне-скоупная просьба («оплати счёт» — нет семьи
    в срезе) → guard НЕ триггерит, отказ отдаётся как есть, один проход."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    scripted = [
        AIMessage(content="Извини, оплачивать счета я не умею."),  # ОТКАЗ, нет семьи
        AIMessage(content="..."),
    ]
    stub = _RecordingStubLLM(scripted)
    reply = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="lazy165-oos", llm=stub, user_text="оплати счёт за свет",
        inbound_message_id="lazy165-oos-msg", channel="react")

    # guard НЕ сработал → ровно ОДИН проход chat (нет повторного bind)
    assert len(stub.binds) == 1, f"guard ложно сработал: {len(stub.binds)} проходов"
    assert "не умею" in (reply or "").lower()


@pytest.mark.asyncio
async def test_repeat_write_after_need_family_no_duplicate(db_session):
    """Срез A пункт 3: повтор разрушающего действия в одном ходу (после добора семьи) НЕ
    задваивает — модель дважды зовёт add_shopping_items с тем же товаром → одна позиция."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    add_molk = {"name": "add_shopping_items",
                "args": {"items": [{"title": "молоко"}]}, "id": None}
    scripted = [
        AIMessage(content="", tool_calls=[{
            "name": "need_family", "args": {"family": "shopping"}, "id": "nf"}]),
        AIMessage(content="", tool_calls=[{**add_molk, "id": "add1"}]),  # 1-й add
        AIMessage(content="", tool_calls=[{**add_molk, "id": "add2"}]),  # повтор того же
        AIMessage(content="Готово."),
    ]
    stub = _RecordingStubLLM(scripted)
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="lazy165-dup", llm=stub, user_text="добавь молоко в покупки",
        inbound_message_id="lazy165-dup-msg", channel="react")

    # title — EncryptedString (недетерминир. шифрование) → фильтровать в Python по расшифровке
    from sreda.db.models.housewife_food import ShoppingListItem
    rows = db_session.query(ShoppingListItem).filter(
        ShoppingListItem.tenant_id == u.tenant_id).all()
    molk = [r for r in rows if r.title == "молоко"]
    assert len(molk) == 1, f"молоко задвоилось/не создано: {len(molk)} строк"


def test_lazy_families_synced_with_need_family_literal():
    """R2 MINOR (субагент): _LAZY_FAMILIES (ре-валидация в run_tools) и Literal-enum схемы
    need_family — два ручных списка; этот тест ловит рассинхрон при добавлении семьи."""
    schema = react_loop.need_family.args_schema.model_json_schema()
    fam = schema["properties"]["family"]
    enum = set(fam.get("enum") or [])
    for node in (fam.get("allOf") or []) + (fam.get("anyOf") or []):  # Literal через allOf/anyOf
        enum |= set(node.get("enum") or [])
    assert enum == set(react_loop._LAZY_FAMILIES), (enum, set(react_loop._LAZY_FAMILIES))


@pytest.mark.asyncio
async def test_need_family_invalid_does_not_load(db_session):
    """R2 (Codex high+medium): невалидная семья (галлюцинация в обход Literal) НЕ грузится —
    набор инструментов не меняется."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    scripted = [
        AIMessage(content="", tool_calls=[{  # мусорная семья
            "name": "need_family", "args": {"family": "utility"}, "id": "nf_bad"}]),
        AIMessage(content="Готово."),
    ]
    stub = _RecordingStubLLM(scripted)
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="lazy165-badfam", llm=stub, user_text="сделай что-нибудь",
        inbound_message_id="lazy165-badfam-msg", channel="react")
    # невалидная семья не загружена → набор на 2-м проходе тот же, что на 1-м
    assert len(stub.binds) >= 2
    assert stub.binds[0] == stub.binds[1], "невалидная семья изменила набор инструментов"


@pytest.mark.asyncio
async def test_need_family_non_string_arg_no_crash(db_session):
    """R2 (Codex high): не-строковый family (list/dict в обход схемы) НЕ роняет ход
    (`x in frozenset` на unhashable дал бы TypeError → «потеряла контекст»)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    scripted = [
        AIMessage(content="", tool_calls=[{  # не-строка (list)
            "name": "need_family", "args": {"family": ["shopping"]}, "id": "nf_list"}]),
        AIMessage(content="Готово."),
    ]
    stub = _RecordingStubLLM(scripted)
    reply = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="lazy165-nonstr", llm=stub, user_text="сделай что-нибудь",
        inbound_message_id="lazy165-nonstr-msg", channel="react")
    assert reply and "потеряла контекст" not in reply  # не упал
    assert stub.binds[0] == stub.binds[1]  # ничего не загружено


@pytest.mark.asyncio
async def test_repeated_need_family_does_not_loop_forever(db_session):
    """R2 (Codex medium+субагент): модель упорно зовёт need_family одной семьи → НЕ
    бесконечный цикл (стоп-узел при лимите проходов), ход завершается грациозно, без
    GraphRecursionError/«потеряла контекст»."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    # стаб ВСЕГДА возвращает need_family(shopping) (повтор последнего сценария)
    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "need_family", "args": {"family": "shopping"}, "id": "loop"}]),
    ])
    reply = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="lazy165-loop", llm=stub, user_text="зациклись",
        inbound_message_id="lazy165-loop-msg", channel="react")
    # завершилось грациозно (не пустой fallback-краш) и проходов не больше лимита
    assert reply
    assert "потеряла контекст" not in (reply or "")
    assert len(stub.binds) <= react_loop._MAX_TURN_PASSES
