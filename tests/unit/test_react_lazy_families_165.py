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


@pytest.fixture(autouse=True)
def _prune_on(monkeypatch):
    """#165 Срез B: флаг обрезки по умолчанию ВЫКЛ → mechanism-тесты гоняем с ВКЛ обрезкой
    (иначе full-bind и посыл «семья не предзагружена» не проверить). Тесты предзагрузки/флага
    переопределяют это сами."""
    monkeypatch.setattr(react_loop, "_is_pruned", lambda _t: True)


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
        # НЕЙТРАЛЬНЫЙ текст: словарь его не матчит → база пуста → проверяем именно ДОБОР
        # (если бы написали «купи молоко», Срез B предзагрузил бы shopping сам).
        user_text="сделай, пожалуйста, вот это",
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
async def test_guard_loads_family_when_model_refuses(db_session, monkeypatch):
    """Срез A пункт 2: модель сама не дозвалась («не умею») → guard догружает семью и ход
    завершается ДЕЛОМ (не молчаливым отказом). Текст нейтральный (база пуста), детектор guard
    мокаем на shopping — изолируем МЕХАНИЗМ guard (в проде детект — тем же словарём)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    monkeypatch.setattr(react_loop, "_guard_family", lambda _t, _a: "shopping")
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
        thread_id="lazy165-guard", llm=stub, user_text="помоги мне, пожалуйста",
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
async def test_guard_full_recovery_then_ends_on_out_of_scope(db_session):
    """#202: вне-скоупная просьба («оплати счёт» — инструмента нет вообще) → отказ → guard делает
    ОДНУ попытку полного восстановления (добор всех семей; вдруг канон-интент мимо словаря) → Среда
    снова отказывает (дела нет) → ход завершается отказом. Цена — один лишний прогон, итог корректный
    («не умею»). Анти-петля: повторно guard не дёргается (guard_full_attempted)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    scripted = [
        AIMessage(content="Извини, оплачивать счета я не умею."),  # отказ → full-recovery
        AIMessage(content="Извини, это я пока не умею."),          # после восстановления — снова отказ
        AIMessage(content="..."),
    ]
    stub = _RecordingStubLLM(scripted)
    reply = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="lazy165-oos", llm=stub, user_text="оплати счёт за свет",
        inbound_message_id="lazy165-oos-msg", channel="react")

    # guard сработал РОВНО раз (full-recovery), затем завершился (анти-петля guard_full_attempted)
    assert len(stub.binds) == 2, f"ожидался один full-recovery retry, не больше: {len(stub.binds)}"
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
        thread_id="lazy165-dup", llm=stub, user_text="помоги мне с делом",
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


# ───────────────────────── #165 Срез B: словарь-предзагрузка + флаг ─────────────────────────

def test_route_families_token_boundary():
    """Срез B: словарь матчит семьи по корню-префиксу токена (не подстрока), top-k."""
    assert "shopping" in react_loop._route_families("купи молоко и хлеб")
    assert "web" in react_loop._route_families("какая погода завтра")
    assert "recipes" in react_loop._route_families("дай рецепт борща")
    assert react_loop._route_families("привет, как дела") == []  # болтовня → пусто
    assert len(react_loop._route_families("купи молоко, дай рецепт, меню на неделю")) <= 2  # top-2
    assert "checklists" in react_loop._route_families("добавь в чек-лист")  # дефис нормализуется
    assert react_loop._route_families("текст", k=0) == []  # k≤0 → пусто (R2 MINOR)


def test_guard_family_picks_first_non_preloaded():
    """R2 (все ревьюеры): guard-добор берёт первую НЕзагруженную семью по ранжированию, а НЕ
    уже-предзагруженный top-1 (иначе guard мёртв). shopping активен → берёт web."""
    fam = react_loop._guard_family("купи молоко и найди магазин рядом", active=["shopping"])
    assert fam == "web", fam  # shopping уже active → первая незагруженная = web
    # если всё уже загружено — None (нечего добирать)
    assert react_loop._guard_family("купи молоко", active=["shopping"]) is None


@pytest.mark.asyncio
async def test_preload_binds_predicted_family_without_need_family(db_session):
    """Срез B: текст про покупки → словарь предзагружает shopping → инструмент доступен СРАЗУ
    (на 1-м проходе), без лишнего need_family-шага."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "add_shopping_items",
            "args": {"items": [{"title": "молоко"}]}, "id": "add"}]),
        AIMessage(content="Готово."),
    ])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="lazy165-preload", llm=stub, user_text="купи молоко",
        inbound_message_id="lazy165-preload-msg", channel="react")
    # shopping предзагружен → доступен уже на ПЕРВОМ bind (need_family не понадобился)
    assert "add_shopping_items" in stub.binds[0]
    from sreda.db.models.housewife_food import ShoppingListItem
    rows = db_session.query(ShoppingListItem).filter(
        ShoppingListItem.tenant_id == u.tenant_id).all()
    assert "молоко" in {r.title for r in rows}


@pytest.mark.asyncio
async def test_flag_off_full_bind_no_pruning(db_session, monkeypatch):
    """Срез B: флаг обрезки ВЫКЛ → full-bind (все семьи привязаны на 1-м проходе) = поведение
    до #165, ноль изменений. (autouse-фикстура _prune_on переопределяется здесь.)"""
    u = seed_telegram_user(db_session)
    db_session.commit()
    monkeypatch.setattr(react_loop, "_is_pruned", lambda _t: False)  # флаг ВЫКЛ
    stub = _RecordingStubLLM([AIMessage(content="Привет!")])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="lazy165-flagoff", llm=stub, user_text="помоги мне",
        inbound_message_id="lazy165-flagoff-msg", channel="react")
    # full-bind: даже на нейтральном тексте семейные инструменты привязаны
    assert "add_shopping_items" in stub.binds[0]
    assert "save_recipe" in stub.binds[0]


@pytest.mark.asyncio
async def test_unkeyed_write_families_always_bound_when_pruning(db_session):
    """Срез B R3-карв-аут (Codex high R2 MAJOR): семьи БЕЗ ключа идемпотентности (остались menu/memory)
    при ВКЛ обрезке остаются ПРИВЯЗАННЫМИ (иначе повтор на recovery-проходе задвоит запись, #163).
    Режем shopping/recipes/checklists/household (есть ключ) + web (чтение).
    #202: recipes+checklists+household оснащены ключами → переехали в prunable."""
    u = seed_telegram_user(db_session)
    db_session.commit()  # _prune_on (autouse) → обрезка ВКЛ
    stub = _RecordingStubLLM([AIMessage(content="Привет!")])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="lazy165-carveout", llm=stub, user_text="привет, как дела",  # нейтральный
        inbound_message_id="lazy165-carveout-msg", channel="react")
    bound = stub.binds[0]
    # unkeyed-write семьи (остались menu/memory) ВСЕГДА привязаны даже при обрезке + нейтральном тексте
    assert "plan_week_menu" in bound       # menu (ещё unkeyed)
    assert "save_core_fact" in bound       # memory (ещё unkeyed)
    # prunable (shopping/recipes/checklists/household/web) на нейтральном тексте НЕ предзагружены (режутся)
    assert "add_shopping_items" not in bound
    assert "save_recipe" not in bound       # #202: recipes idempotent → prunable
    assert "create_checklist" not in bound  # #202: checklists idempotent → prunable
    assert "add_family_members" not in bound  # #202: household idempotent → prunable
    assert "web_search" not in bound


@pytest.mark.asyncio
async def test_guard_suppressed_after_unkeyed_write(db_session, monkeypatch):
    """R4 (Codex medium): после выполнения инструмента unkeyed-семьи в ходу guard ОТКЛЮЧЁН —
    повторный recovery-проход мог бы задвоить запись. (need_family остаётся доступен моделью.)"""
    u = seed_telegram_user(db_session)
    db_session.commit()
    monkeypatch.setattr(react_loop, "_guard_family", lambda _t, _a: "web")  # guard БЫ выбрал web
    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[{  # пасс1: инструмент unkeyed-семьи (menu; #202 household уже keyed)
            "name": "list_menu", "args": {}, "id": "lm"}]),
        AIMessage(content="Извини, искать в сети пока не умею."),  # пасс2: отказ (web-ish)
        AIMessage(content="..."),
    ])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="lazy165-unkeyed-guard", llm=stub, user_text="покажи меню",
        inbound_message_id="lazy165-unkeyed-guard-msg", channel="react")
    # guard НЕ сработал (wrote_unkeyed) → нет 3-го прохода с web; web не догружен
    assert len(stub.binds) == 2, f"guard сработал после unkeyed-write: {len(stub.binds)} проходов"
    assert all("web_search" not in b for b in stub.binds)


@pytest.mark.asyncio
async def test_need_family_loads_checklists_reachable_202(db_session):
    """#202 (Codex medium R1 CRITICAL — reject): checklists стал prunable, НО достижим через
    need_family на pruned-тенанте (роутер-промах канон-интента = лишний шаг, НЕ недоступность,
    строка ~740 'НЕ отказ'). need_family(checklists) → create_checklist доступен и выполняется."""
    u = seed_telegram_user(db_session)
    db_session.commit()  # _prune_on (autouse) → обрезка ВКЛ
    scripted = [
        AIMessage(content="", tool_calls=[{
            "name": "need_family", "args": {"family": "checklists"}, "id": "nf"}]),
        AIMessage(content="", tool_calls=[{
            "name": "create_checklist", "args": {"title": "план кроя"}, "id": "cc"}]),
        AIMessage(content="Готово."),
    ]
    stub = _RecordingStubLLM(scripted)
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="lazy202-cl", llm=stub, user_text="сделай вот это",
        inbound_message_id="lazy202-cl-msg", channel="react")
    assert "create_checklist" not in stub.binds[0], "checklists prunable → не предзагружен на нейтральном"
    assert len(stub.binds) >= 2 and "create_checklist" in stub.binds[1], (
        "после need_family(checklists) create_checklist должен быть в наборе")
    from sreda.db.models.checklists import Checklist
    rows = db_session.query(Checklist).filter(Checklist.tenant_id == u.tenant_id).all()
    assert any((c.title or "") == "план кроя" for c in rows), "чек-лист не создан через need_family"


@pytest.mark.asyncio
async def test_guard_full_recovery_on_router_miss_refusal_202(db_session):
    """#202 (Codex medium R2 CRITICAL): канон-интент мимо словаря-роутера («план кроя» НЕ матчит
    checklists-корни) на pruned-тенанте → модель отказывает БЕЗ need_family → guard делает FULL-recovery
    (добирает ВСЕ ленивые семьи) → на ретрае create_checklist доступен и выполняется. Детерминированно,
    без scripted need_family (доказывает ВОССТАНОВЛЕНИЕ, не только механизм добора)."""
    u = seed_telegram_user(db_session)
    db_session.commit()  # _prune_on (autouse) → обрезка ВКЛ
    scripted = [
        # пасс 1: checklists не предзагружен (роутер промахнулся) → модель отказывает БЕЗ инструмента
        AIMessage(content="Извини, чек-листы пока не умею."),
        # пасс 2: после guard FULL-recovery create_checklist в наборе → реальный вызов
        AIMessage(content="", tool_calls=[{
            "name": "create_checklist", "args": {"title": "план кроя"}, "id": "cc"}]),
        AIMessage(content="Готово."),
    ]
    stub = _RecordingStubLLM(scripted)
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="lazy202-full", llm=stub, user_text="план кроя",
        inbound_message_id="lazy202-full-msg", channel="react")
    assert "create_checklist" not in stub.binds[0], "роутер промахнулся по «план кроя» → не предзагружен"
    assert len(stub.binds) >= 2 and "create_checklist" in stub.binds[1], (
        "после guard FULL-recovery (отказ + промах роутера) create_checklist должен стать доступен")
    from sreda.db.models.checklists import Checklist
    rows = db_session.query(Checklist).filter(Checklist.tenant_id == u.tenant_id).all()
    assert any((c.title or "") == "план кроя" for c in rows), "чек-лист не создан после full-recovery"


@pytest.mark.asyncio
async def test_guard_suppressed_after_core_write_no_dup_202(db_session):
    """#202 (Codex medium R3 CRITICAL): после core-записи (add_task — задача без даты, нет
    семантического дедупа) отказ на второй интент → guard/full-recovery ПОДАВЛЕН (wrote_unkeyed),
    иначе ретрай пере-создал бы задачу. Задача РОВНО одна; full-recovery-прохода нет (binds==2)."""
    u = seed_telegram_user(db_session)
    db_session.commit()  # _prune_on (autouse) → обрезка ВКЛ
    scripted = [
        AIMessage(content="", tool_calls=[{
            "name": "add_task", "args": {"title": "купить хлеб"}, "id": "at"}]),  # core durable-write
        AIMessage(content="Задачу добавила. А рецепт пока не умею."),  # отказ ПОСЛЕ записи
        AIMessage(content="..."),
    ]
    stub = _RecordingStubLLM(scripted)
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="lazy202-core", llm=stub, user_text="добавь задачу купить хлеб",
        inbound_message_id="lazy202-core-msg", channel="react")
    assert len(stub.binds) == 2, (
        f"после core-записи guard НЕ должен восстанавливать (нет 3-го прохода): {len(stub.binds)}")
    from sreda.db.models.tasks import Task
    tasks = db_session.query(Task).filter(Task.tenant_id == u.tenant_id).all()
    assert len(tasks) == 1, f"задача РОВНО одна (без дубля от recovery-ретрая): {len(tasks)}"
