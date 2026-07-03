"""#213 Срез B - RED-контракт: предслой query_kind + soft cross-check + write-enforcement.

Пинит чеклист приёмки (plans/213-acceptance-draft.md): ядро пп.3 (негативы overview),
5 (mismatch «кино→машина» - отказ, не исполнение), 8 (write-enforcement source_result_id);
инж. пп.11 (search-хедж), 12 (узкий редирект), 13 (fail-open), 14 (origin-гейт),
15 (mixed), 19 (ON/OFF матрица - второй флаг).

Флаги: SREDA_CHECKLIST_UNIFIED (срез A) + SREDA_CHECKLIST_QUERYKIND (срез B).
Cross-check живёт ТОЛЬКО при ОБОИХ ON. Всё красное до реализации среза B.
"""

from __future__ import annotations

import pytest

from langchain_core.messages import AIMessage

from sreda.runtime import react_loop
from sreda.services.checklists import ChecklistService
from tests.unit.conftest import seed_telegram_user
from tests.unit.test_213_get_checklist_slice_a import (
    _RecordingStubLLM,
    _housewife_tools,
    _seed_kino,
    _tool_message_for,
)


def _flags(monkeypatch, *, unified: bool, querykind: bool, preflight: bool = True) -> None:
    import sreda.config.settings as sm

    monkeypatch.setenv("SREDA_CHECKLIST_UNIFIED", "1" if unified else "0")
    monkeypatch.setenv("SREDA_CHECKLIST_QUERYKIND", "1" if querykind else "0")
    monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "1" if preflight else "0")
    sm.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# A. Классификатор query_kind (чистый юнит, react_preflight)
# ---------------------------------------------------------------------------


def _classify(text: str):
    from sreda.runtime.react_preflight import classify_checklist_query

    return classify_checklist_query(text)


def test_kind_items_with_name():
    q = _classify("покажи список кино")
    assert q is not None and q.kind == "items"
    assert q.name_span == "кино"
    assert q.confidence == "high"


def test_kind_items_variants():
    for text, span in [
        ("что осталось в списке поход", "поход"),
        ("покажи план кроя", "кроя"),
        ("что в списке машина", "машина"),
    ]:
        q = _classify(text)
        assert q is not None and q.kind == "items", text
        assert q.name_span == span, text


def test_kind_overview_plural_and_all():
    for text in ("какие у меня списки", "покажи все списки", "покажи все мои планы",
                 "сколько списков я завёл", "какие списки"):
        q = _classify(text)
        assert q is not None and q.kind == "overview", text


def test_kind_bare_singular_is_items_without_name():
    """Голое «покажи список» - items без имени (НЕ overview): уточнение, не все списки."""
    q = _classify("покажи список")
    assert q is not None and q.kind == "items"
    assert not q.name_span


def test_kind_search():
    for text in ("найди пункт лопата", "в каком списке у меня лаванда", "где записана колодка"):
        q = _classify(text)
        assert q is not None and q.kind == "search", text


def test_kind_mixed():
    q = _classify("покажи список кино и какие ещё списки есть")
    assert q is not None and q.kind == "mixed"


def test_kind_none_for_write_turns():
    """Write-ходы предслой НЕ гейтит (подготовительные read-вызовы легитимны)."""
    for text in ("добавь в список кино фильм мастодонт", "отметь лопату",
                 "удали пункт стекло", "создай список дача", "архивируй список поход"):
        assert _classify(text) is None, text


def test_kind_none_for_non_checklist():
    for text in ("какая погода", "напомни завтра в 9", "привет", "список покупок покажи"):
        assert _classify(text) is None, text


# --- фиксы R2 Claude в классификаторе -------------------------------------


def test_kind_write_verbs_extended():
    """R2 B2: расширенный инвентарь write-глаголов → None (write-ход не гейтим)."""
    for text in ("убери лопату из списка кино", "зачеркни стекло в списке машина",
                 "обнови пункт в плане кроя", "очисти список поход",
                 "дополни список кино фильмом", "поставь галочку на лопате",
                 "сними отметку с колодок", "поменяй местами пункты",
                 "восстанови удалённый пункт", "верни лопату в список"):
        assert _classify(text) is None, text


def test_kind_section_word_not_captured_as_name():
    """R2 M1: «покажи список дел» — «дел» это раздел-слово, не имя → overview, не items/«дел»."""
    q = _classify("покажи список дел")
    assert q is not None and q.kind == "overview", (q.kind, q.name_span)


def test_kind_planov_overview():
    """R2 M3: «сколько у меня планов» → overview (форма «планов» распознана)."""
    q = _classify("сколько у меня планов")
    assert q is not None and q.kind == "overview", (q.kind if q else None)


# ---------------------------------------------------------------------------
# B. Cross-check в tool-node (ON/ON): mismatch, редирект, search-гейт, зеркало
# ---------------------------------------------------------------------------


async def _turn(db_session, u, stub, text, thread):
    return await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread, llm=stub, user_text=text,
        inbound_message_id=f"{thread}-msg", channel="react",
    )


@pytest.mark.asyncio
async def test_mismatch_overview_call_on_items_intent_refused(db_session, monkeypatch):
    """Ядро инцидента: items-интент, модель зовёт overview → структурный отказ, НЕ обзор."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)
    svc = ChecklistService(db_session)
    svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Поход")
    db_session.commit()

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "get_checklist", "args": {"mode": "overview"}, "id": "call_ov",
        }]),
        AIMessage(content="Уточню."),
    ])
    await _turn(db_session, u, stub, "покажи список кино", "213b-mm-1")
    tm = _tool_message_for(stub, "call_ov")
    assert tm is not None
    content = str(tm.content)
    assert "mode_mismatch" in content, content
    assert "Поход" not in content and "Кино к просмотру" not in content, (
        f"отказ не должен нести обзор (класс #213): {content}")


@pytest.mark.asyncio
async def test_conflicting_resolvable_names_refused(db_session, monkeypatch):
    """Приёмка п.5: предслой high-conf «кино», модель зовёт «машина» (обе резолвятся) →
    отказ с подсказкой, НЕ исполнение."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)
    svc = ChecklistService(db_session)
    m = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Машина")
    svc.add_items(list_id=m.id, items=["Колодки"])
    db_session.commit()

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "get_checklist", "args": {"mode": "items", "name": "машина"},
            "id": "call_conf",
        }]),
        AIMessage(content="Уточню."),
    ])
    await _turn(db_session, u, stub, "покажи список кино", "213b-conf-1")
    tm = _tool_message_for(stub, "call_conf")
    assert tm is not None
    content = str(tm.content)
    assert "name_conflict" in content, content
    assert "Колодки" not in content, f"не тот список НЕ исполняется: {content}"


@pytest.mark.asyncio
async def test_redirect_fills_missing_name(db_session, monkeypatch):
    """Приёмка п.12: no-name items-вызов при high-conf resolved_name → редирект имени
    (исполнение с именем предслоя), не name_required."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "get_checklist", "args": {"mode": "items"}, "id": "call_rd",
        }]),
        AIMessage(content="Вот."),
    ])
    await _turn(db_session, u, stub, "покажи список кино", "213b-rd-1")
    tm = _tool_message_for(stub, "call_rd")
    assert tm is not None
    content = str(tm.content)
    assert "result_type=items" in content and "Скорпион" in content, (
        f"редирект должен исполнить с именем предслоя: {content}")


@pytest.mark.asyncio
async def test_search_call_on_items_intent_refused(db_session, monkeypatch):
    """Приёмка п.11: items-интент + паразитный list_checklist_items → отказ,
    нет конкурирующего search-результата."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[
            {"name": "get_checklist", "args": {"mode": "items", "name": "кино"},
             "id": "call_ok"},
            {"name": "list_checklist_items", "args": {"title_match": "кино"},
             "id": "call_par"},
        ]),
        AIMessage(content="Вот список."),
    ])
    await _turn(db_session, u, stub, "покажи список кино", "213b-sh-1")
    ok = _tool_message_for(stub, "call_ok")
    par = _tool_message_for(stub, "call_par")
    assert ok is not None and "result_type=items" in str(ok.content)
    assert par is not None
    assert "mode_mismatch" in str(par.content), par.content
    assert "@" not in str(par.content), f"search-строк быть не должно: {par.content}"


@pytest.mark.asyncio
async def test_mirror_items_call_on_overview_intent_refused(db_session, monkeypatch):
    """Матрица «ЗЕРКАЛО»: overview-интент + паразитный items-вызов → отказ items,
    рендер по overview."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[
            {"name": "get_checklist", "args": {"mode": "overview"}, "id": "call_ov2"},
            {"name": "get_checklist", "args": {"mode": "items", "name": "кино"},
             "id": "call_it2"},
        ]),
        AIMessage(content="Вот списки."),
    ])
    await _turn(db_session, u, stub, "какие у меня списки", "213b-mir-1")
    ov = _tool_message_for(stub, "call_ov2")
    it = _tool_message_for(stub, "call_it2")
    assert ov is not None and "result_type=overview" in str(ov.content)
    assert it is not None and "mode_mismatch" in str(it.content), it.content
    assert "Скорпион" not in str(it.content)


@pytest.mark.asyncio
async def test_mixed_allows_both(db_session, monkeypatch):
    """Приёмка п.15: mixed-интент разрешает items И overview (различимость - envelope)."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[
            {"name": "get_checklist", "args": {"mode": "items", "name": "кино"},
             "id": "call_mx1"},
            {"name": "get_checklist", "args": {"mode": "overview"}, "id": "call_mx2"},
        ]),
        AIMessage(content="Вот."),
    ])
    await _turn(db_session, u, stub, "покажи список кино и какие ещё списки есть", "213b-mx-1")
    a = _tool_message_for(stub, "call_mx1")
    b = _tool_message_for(stub, "call_mx2")
    assert a is not None and "result_type=items" in str(a.content)
    assert b is not None and "result_type=overview" in str(b.content), (
        f"mixed разрешает overview-часть: {b.content}")


@pytest.mark.asyncio
async def test_fail_open_without_querykind_flag(db_session, monkeypatch):
    """Приёмка п.13/19 (ON/OFF): unified=ON, querykind=OFF → cross-check молчит,
    голая схема среза A работает."""
    _flags(monkeypatch, unified=True, querykind=False)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)
    svc = ChecklistService(db_session)
    svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Поход")
    db_session.commit()

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "get_checklist", "args": {"mode": "overview"}, "id": "call_fo",
        }]),
        AIMessage(content="Вот."),
    ])
    await _turn(db_session, u, stub, "покажи список кино", "213b-fo-1")
    tm = _tool_message_for(stub, "call_fo")
    assert tm is not None
    content = str(tm.content)
    assert "result_type=overview" in content, (
        f"fail-open: без флага среза B mismatch НЕ гейтится: {content}")


@pytest.mark.asyncio
async def test_write_turn_not_gated(db_session, monkeypatch):
    """Write-ход («отметь лопату»): подготовительный search-вызов НЕ гейтится."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    cl = _seed_kino(db_session, u)
    svc = ChecklistService(db_session)
    svc.add_items(list_id=cl.id, items=["лопата"])
    db_session.commit()

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "list_checklist_items", "args": {"title_match": "лопата"},
            "id": "call_wr",
        }]),
        AIMessage(content="Нашла, отмечаю."),
    ])
    await _turn(db_session, u, stub, "отметь лопату", "213b-wr-1")
    tm = _tool_message_for(stub, "call_wr")
    assert tm is not None
    content = str(tm.content)
    assert "mode_mismatch" not in content, f"write-ход не гейтится: {content}"
    assert "лопата" in content


# ---------------------------------------------------------------------------
# C. Write-enforcement source_result_id (приёмка п.8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_enforcement_two_items_results_requires_binding(db_session, monkeypatch):
    """Два items-result в ходе + mark без source_result_id → уточнение, НЕ write."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    svc = ChecklistService(db_session)
    a = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Кино к просмотру")
    (ia,), _ = svc.add_items(list_id=a.id, items=["Дюна"])
    b = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Машина")
    (ib,), _ = svc.add_items(list_id=b.id, items=["Колодки"])
    db_session.commit()

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[
            {"name": "get_checklist", "args": {"mode": "items", "name": "кино"},
             "id": "c1"},
            {"name": "get_checklist", "args": {"mode": "items", "name": "машина"},
             "id": "c2"},
        ]),
        AIMessage(content="", tool_calls=[{
            "name": "mark_checklist_item_done", "args": {"item_id": ia.id}, "id": "c3",
        }]),
        AIMessage(content="Готово."),
    ])
    await _turn(db_session, u, stub, "что в списках кино и машина, отметь первый",
                "213b-we-1")
    tm = _tool_message_for(stub, "c3")
    assert tm is not None
    content = str(tm.content)
    assert "source_result_required" in content, content
    db_session.expire_all()
    from sreda.db.models.checklists import ChecklistItem
    assert db_session.get(ChecklistItem, ia.id).status == "pending", (
        "write НЕ должен исполниться без привязки")


@pytest.mark.asyncio
async def test_write_enforcement_single_result_passes(db_session, monkeypatch):
    """Один items-result в ходе → mark по его item_id проходит без source_result_id."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    cl = _seed_kino(db_session, u)
    svc = ChecklistService(db_session)
    (it,), _ = svc.add_items(list_id=cl.id, items=["лопата"])
    db_session.commit()

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "get_checklist", "args": {"mode": "items", "name": "кино"},
            "id": "d1",
        }]),
        AIMessage(content="", tool_calls=[{
            "name": "mark_checklist_item_done", "args": {"item_id": it.id}, "id": "d2",
        }]),
        AIMessage(content="Отметила."),
    ])
    await _turn(db_session, u, stub, "покажи список кино и отметь лопату", "213b-we-2")
    tm = _tool_message_for(stub, "d2")
    assert tm is not None
    assert "ok:done" in str(tm.content), tm.content
    db_session.expire_all()
    from sreda.db.models.checklists import ChecklistItem
    assert db_session.get(ChecklistItem, it.id).status == "done"


@pytest.mark.asyncio
async def test_write_between_reads_same_batch_gated(db_session, monkeypatch):
    """R1 high MAJOR: батч параллелен - write МЕЖДУ двумя read в одном AIMessage
    гейтится (видит и неисполненные read'ы батча)."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    svc = ChecklistService(db_session)
    a = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Кино к просмотру")
    (ia,), _ = svc.add_items(list_id=a.id, items=["Дюна"])
    b = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Машина")
    svc.add_items(list_id=b.id, items=["Колодки"])
    db_session.commit()

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[
            {"name": "get_checklist", "args": {"mode": "items", "name": "кино"}, "id": "sb1"},
            {"name": "mark_checklist_item_done", "args": {"item_id": ia.id}, "id": "sb2"},
            {"name": "get_checklist", "args": {"mode": "items", "name": "машина"}, "id": "sb3"},
        ]),
        AIMessage(content="Готово."),
    ])
    await _turn(db_session, u, stub, "что в списках кино и машина, отметь первый",
                "213b-sb-1")
    tm = _tool_message_for(stub, "sb2")
    assert tm is not None
    assert "source_result_required" in str(tm.content), tm.content
    db_session.expire_all()
    from sreda.db.models.checklists import ChecklistItem
    assert db_session.get(ChecklistItem, ia.id).status == "pending", (
        "write между read'ами одного батча не должен исполниться")


@pytest.mark.asyncio
async def test_querykind_inert_without_preflight(db_session, monkeypatch):
    """R1 medium MAJOR: unified=ON + querykind=ON, но preflight=OFF → срез B молчит
    (mismatch НЕ гейтится, enforcement не работает - контракт флага)."""
    _flags(monkeypatch, unified=True, querykind=True, preflight=False)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)
    svc = ChecklistService(db_session)
    svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Поход")
    db_session.commit()

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "get_checklist", "args": {"mode": "overview"}, "id": "pf1",
        }]),
        AIMessage(content="Вот."),
    ])
    await _turn(db_session, u, stub, "покажи список кино", "213b-pf-1")
    tm = _tool_message_for(stub, "pf1")
    assert tm is not None
    assert "result_type=overview" in str(tm.content), (
        f"без preflight срез B инертен: {tm.content}")


class _DynamicStubLLM:
    """Стаб, чьи tool_calls вычисляются из УВИДЕННЫХ ToolMessage (для позитива
    write-enforcement: подставить source_result_id + item_id из показанного items-result)."""

    def __init__(self, planner):
        self._planner = planner  # callable(seen_tool_messages) -> AIMessage
        self.seen_messages: list[list] = []

    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages):  # noqa: ANN001
        self.seen_messages.append(list(messages))
        from langchain_core.messages import ToolMessage
        tms = [m for m in messages if isinstance(m, ToolMessage)]
        return self._planner(tms)


def _parse_result_id(content: str) -> str:
    import re as _re
    m = _re.search(r"result_id=(\S+)", content or "")
    return m.group(1) if m else ""


def _parse_first_item_id(content: str) -> str:
    import re as _re
    m = _re.search(r"\[(clitem_[0-9a-f]+)\]", content or "")
    return m.group(1) if m else ""


@pytest.mark.asyncio
async def test_write_enforcement_positive_binding_executes(db_session, monkeypatch):
    """R2 Claude B3 (приёмка п.8 позитив): 2 items-result + write С правильной привязкой
    (source_result_id + item_id из ТОГО результата) → исполняется."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    svc = ChecklistService(db_session)
    a = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Кино к просмотру")
    (ia,), _ = svc.add_items(list_id=a.id, items=["Дюна"])
    b = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Машина")
    svc.add_items(list_id=b.id, items=["Колодки"])
    db_session.commit()
    ia_id = ia.id

    from langchain_core.messages import AIMessage as _AI

    def _planner(tms):
        if not tms:  # проход 1: два items-вызова
            return _AI(content="", tool_calls=[
                {"name": "get_checklist", "args": {"mode": "items", "name": "кино"}, "id": "p1"},
                {"name": "get_checklist", "args": {"mode": "items", "name": "машина"}, "id": "p2"},
            ])
        done = {m.tool_call_id for m in tms}
        if "p3" not in done:  # проход 2: write с привязкой к result_id списка «Кино»
            kino_rid = ""
            for m in tms:
                c = str(m.content)
                if "Кино к просмотру" in c and "result_type=items" in c:
                    kino_rid = _parse_result_id(c)
            return _AI(content="", tool_calls=[{
                "name": "mark_checklist_item_done",
                "args": {"item_id": ia_id, "source_result_id": kino_rid}, "id": "p3",
            }])
        return _AI(content="Отметила.")

    stub = _DynamicStubLLM(_planner)
    await _turn(db_session, u, stub, "что в списках кино и машина, отметь первый из кино",
                "213b-pos-1")
    tm = _tool_message_for(stub, "p3")
    assert tm is not None, "write-проход не дошёл"
    assert "ok:done" in str(tm.content), tm.content
    db_session.expire_all()
    from sreda.db.models.checklists import ChecklistItem
    assert db_session.get(ChecklistItem, ia_id).status == "done"


@pytest.mark.asyncio
async def test_write_enforcement_wrong_binding_refused(db_session, monkeypatch):
    """R2 Claude B3: source_result_id указывает на ДРУГОЙ результат (item_id не из него) →
    отказ, write не исполняется."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    svc = ChecklistService(db_session)
    a = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Кино к просмотру")
    (ia,), _ = svc.add_items(list_id=a.id, items=["Дюна"])
    b = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Машина")
    (ib,), _ = svc.add_items(list_id=b.id, items=["Колодки"])
    db_session.commit()
    ia_id, ib_id = ia.id, ib.id

    from langchain_core.messages import AIMessage as _AI

    def _planner(tms):
        if not tms:
            return _AI(content="", tool_calls=[
                {"name": "get_checklist", "args": {"mode": "items", "name": "кино"}, "id": "w1"},
                {"name": "get_checklist", "args": {"mode": "items", "name": "машина"}, "id": "w2"},
            ])
        done = {m.tool_call_id for m in tms}
        if "w3" not in done:
            # берём result_id списка «Машина», но item_id из «Кино» — несоответствие
            mash_rid = ""
            for m in tms:
                c = str(m.content)
                if "Машина" in c and "result_type=items" in c:
                    mash_rid = _parse_result_id(c)
            return _AI(content="", tool_calls=[{
                "name": "mark_checklist_item_done",
                "args": {"item_id": ia_id, "source_result_id": mash_rid}, "id": "w3",
            }])
        return _AI(content="Уточню.")

    stub = _DynamicStubLLM(_planner)
    await _turn(db_session, u, stub, "что в списках кино и машина, отметь первый", "213b-wb-1")
    tm = _tool_message_for(stub, "w3")
    assert tm is not None
    assert "source_result" in str(tm.content) and "ok:done" not in str(tm.content), tm.content
    db_session.expire_all()
    from sreda.db.models.checklists import ChecklistItem
    assert db_session.get(ChecklistItem, ia_id).status == "pending"


@pytest.mark.asyncio
async def test_compound_items_items_both_execute(db_session, monkeypatch):
    """R2 Claude B1 (приёмка п.15 items+items): «что в списке кино и в списке машина» —
    ОБА вызова исполняются, второй (машина) НЕ отказывается name_conflict'ом."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    svc = ChecklistService(db_session)
    a = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Кино к просмотру")
    svc.add_items(list_id=a.id, items=["Дюна"])
    b = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Машина")
    svc.add_items(list_id=b.id, items=["Колодки"])
    db_session.commit()

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[
            {"name": "get_checklist", "args": {"mode": "items", "name": "кино"}, "id": "cm1"},
            {"name": "get_checklist", "args": {"mode": "items", "name": "машина"}, "id": "cm2"},
        ]),
        AIMessage(content="Вот оба."),
    ])
    await _turn(db_session, u, stub, "что в списке кино и в списке машина", "213b-cmp-1")
    a1 = _tool_message_for(stub, "cm1")
    a2 = _tool_message_for(stub, "cm2")
    assert a1 is not None and "Дюна" in str(a1.content)
    assert a2 is not None and "name_conflict" not in str(a2.content), a2.content
    assert "Колодки" in str(a2.content), f"второй список должен исполниться: {a2.content}"


@pytest.mark.asyncio
async def test_redirect_nonresolving_name(db_session, monkeypatch):
    """R2 Claude B5 (приёмка п.12): опечатка «кинооо» (не резолвится) при high-conf span
    «кино» → редирект на span, исполнение, НЕ not_found."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "get_checklist", "args": {"mode": "items", "name": "кинооо"}, "id": "nr1",
        }]),
        AIMessage(content="Вот."),
    ])
    await _turn(db_session, u, stub, "покажи список кино", "213b-nr-1")
    tm = _tool_message_for(stub, "nr1")
    assert tm is not None
    assert "result_type=items" in str(tm.content) and "Скорпион" in str(tm.content), (
        f"нерезолвящееся имя должно редиректиться на span: {tm.content}")


@pytest.mark.asyncio
async def test_redirect_does_not_overwrite_valid_name(db_session, monkeypatch):
    """R2 Claude B5-граница: РЕЗОЛВЯЩЕЕСЯ имя модели, названное юзером, НЕ перезаписывается."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)
    svc = ChecklistService(db_session)
    p = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Поход")
    svc.add_items(list_id=p.id, items=["Палатка"])
    db_session.commit()

    # юзер назвал ОБА («кино и поход»), span=«кино», модель зовёт «поход» — легитимно, не перезап.
    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "get_checklist", "args": {"mode": "items", "name": "поход"}, "id": "vn1",
        }]),
        AIMessage(content="Вот."),
    ])
    await _turn(db_session, u, stub, "что в списке кино и поход", "213b-vn-1")
    tm = _tool_message_for(stub, "vn1")
    assert tm is not None
    assert "Палатка" in str(tm.content), f"имя, названное юзером, не перезаписывается: {tm.content}"


@pytest.mark.asyncio
async def test_write_enforcement_inert_without_preflight(db_session, monkeypatch):
    """R2 Codex high+medium MAJOR: write-enforcement ТОЖЕ гейтится preflight -
    при preflight=OFF два items-result + write без source_result_id проходит по легаси."""
    _flags(monkeypatch, unified=True, querykind=True, preflight=False)
    u = seed_telegram_user(db_session)
    svc = ChecklistService(db_session)
    a = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Кино к просмотру")
    (ia,), _ = svc.add_items(list_id=a.id, items=["Дюна"])
    b = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Машина")
    svc.add_items(list_id=b.id, items=["Колодки"])
    db_session.commit()

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[
            {"name": "show_checklist", "args": {"list_id_or_title": "кино"}, "id": "wp1"},
            {"name": "show_checklist", "args": {"list_id_or_title": "машина"}, "id": "wp2"},
        ]),
        AIMessage(content="", tool_calls=[{
            "name": "mark_checklist_item_done", "args": {"item_id": ia.id}, "id": "wp3",
        }]),
        AIMessage(content="Готово."),
    ])
    await _turn(db_session, u, stub, "что в списках кино и машина, отметь первый",
                "213b-wp-1")
    tm = _tool_message_for(stub, "wp3")
    assert tm is not None
    assert "source_result_required" not in str(tm.content), (
        f"без preflight write-enforcement инертен: {tm.content}")
    assert "ok:done" in str(tm.content), tm.content
    db_session.expire_all()
    from sreda.db.models.checklists import ChecklistItem
    assert db_session.get(ChecklistItem, ia.id).status == "done"


@pytest.mark.asyncio
async def test_write_enforcement_off_when_flags_off(db_session, monkeypatch):
    """OFF/OFF: enforcement молчит - легаси-поведение write-инструментов."""
    _flags(monkeypatch, unified=False, querykind=False, preflight=False)
    u = seed_telegram_user(db_session)
    cl = _seed_kino(db_session, u)
    svc = ChecklistService(db_session)
    (it,), _ = svc.add_items(list_id=cl.id, items=["лопата"])
    db_session.commit()

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "mark_checklist_item_done", "args": {"item_id": it.id}, "id": "e1",
        }]),
        AIMessage(content="Отметила."),
    ])
    await _turn(db_session, u, stub, "отметь лопату", "213b-we-3")
    tm = _tool_message_for(stub, "e1")
    assert tm is not None
    assert "ok:done" in str(tm.content), tm.content


# --- фиксы R3 Codex (границы токена, безусловный strip) --------------------


def test_token_in_text_boundaries():
    """R3 medium: «ход» не считается названным в «поход» (границы токена, не substring)."""
    from sreda.runtime.react_loop import _token_in_text
    assert _token_in_text("поход", "что в списке кино и поход")
    assert not _token_in_text("ход", "покажи список поход")
    assert _token_in_text("кино", "покажи список кино")
    assert not _token_in_text("ино", "покажи список кино")


@pytest.mark.asyncio
async def test_compound_substring_name_not_false_matched(db_session, monkeypatch):
    """R3 medium: список «ход», запрос про «поход» — вызов «ход» (не названный юзером)
    при span=«поход» конфликтует (не проходит по ложному substring)."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    svc = ChecklistService(db_session)
    p = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="поход")
    svc.add_items(list_id=p.id, items=["Палатка"])
    h = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="ход")
    svc.add_items(list_id=h.id, items=["Шахматы"])
    db_session.commit()

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "get_checklist", "args": {"mode": "items", "name": "ход"}, "id": "ts1",
        }]),
        AIMessage(content="Уточню."),
    ])
    await _turn(db_session, u, stub, "покажи список поход", "213b-ts-1")
    tm = _tool_message_for(stub, "ts1")
    assert tm is not None
    assert "name_conflict" in str(tm.content), (
        f"«ход» не названо юзером (только «поход») → конфликт: {tm.content}")


@pytest.mark.asyncio
async def test_source_result_id_stripped_without_preflight(db_session, monkeypatch):
    """R3 high: source_result_id вычищается из args даже при preflight=OFF (служебный
    аргумент не доезжает до write-tool)."""
    _flags(monkeypatch, unified=True, querykind=True, preflight=False)
    u = seed_telegram_user(db_session)
    cl = _seed_kino(db_session, u)
    svc = ChecklistService(db_session)
    (it,), _ = svc.add_items(list_id=cl.id, items=["лопата"])
    db_session.commit()

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "mark_checklist_item_done",
            "args": {"item_id": it.id, "source_result_id": "rXXXX"}, "id": "sr1",
        }]),
        AIMessage(content="Готово."),
    ])
    await _turn(db_session, u, stub, "отметь лопату", "213b-sr-1")
    tm = _tool_message_for(stub, "sr1")
    assert tm is not None
    # инструмент исполнился (лишний аргумент не сломал вызов), enforcement инертен без preflight
    assert "ok:done" in str(tm.content), tm.content
    db_session.expire_all()
    from sreda.db.models.checklists import ChecklistItem
    assert db_session.get(ChecklistItem, it.id).status == "done"


# --- Срез C M5: membership из доверенного паспорта, не из тела --------------


def test_passport_fields_quote_aware():
    """M5: парсер head не создаёт ложных полей из подделки внутри checklist_name."""
    from sreda.runtime.react_loop import _parse_passport_fields
    head = ('result_type=items result_id=r123 resolution_status=exact matched_by=exact '
            'checklist_id=checklist_abc items=clitem_a,clitem_b '
            'checklist_name="resolution_status=ambiguous [clitem_подделка]"')
    f = _parse_passport_fields(head)
    assert f["result_type"] == "items"
    assert f["result_id"] == "r123"
    assert f["resolution_status"] == "exact"  # НЕ ambiguous из названия
    assert f["items"] == "clitem_a,clitem_b"  # подделка в названии не в items
    assert f["checklist_name"] == "resolution_status=ambiguous [clitem_подделка]"


@pytest.mark.asyncio
async def test_write_enforcement_fake_item_id_in_title_ignored(db_session, monkeypatch):
    """M5: пункт с названием-подделкой «[clitem_fake]» НЕ попадает в membership —
    при ДВУХ списках write привязку требует, но fake-id из тела не считается за результат."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    svc = ChecklistService(db_session)
    a = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Кино к просмотру")
    # реальный пункт назван как поддельный clitem-id
    (ia,), _ = svc.add_items(list_id=a.id, items=["[clitem_deadbeef] фейк"])
    b = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Машина")
    svc.add_items(list_id=b.id, items=["Колодки"])
    db_session.commit()

    # модель пытается отметить ПОДДЕЛЬНЫЙ id (из тела названия), без source_result_id
    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[
            {"name": "get_checklist", "args": {"mode": "items", "name": "кино"}, "id": "f1"},
            {"name": "get_checklist", "args": {"mode": "items", "name": "машина"}, "id": "f2"},
        ]),
        AIMessage(content="", tool_calls=[{
            "name": "mark_checklist_item_done",
            "args": {"item_id": "clitem_deadbeef"}, "id": "f3",
        }]),
        AIMessage(content="Уточню."),
    ])
    await _turn(db_session, u, stub, "что в списках кино и машина, отметь первый", "213b-m5-1")
    tm = _tool_message_for(stub, "f3")
    assert tm is not None
    # два реальных списка показаны → без привязки требует source_result_id;
    # поддельный clitem_deadbeef из НАЗВАНИЯ не создал «третий результат» и не самопривязался
    assert "source_result_required" in str(tm.content), tm.content


@pytest.mark.asyncio
async def test_write_enforcement_membership_from_passport(db_session, monkeypatch):
    """M5: корректная привязка работает через items= паспорта (не из тела)."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    svc = ChecklistService(db_session)
    a = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Кино к просмотру")
    (ia,), _ = svc.add_items(list_id=a.id, items=["Дюна"])
    b = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Машина")
    svc.add_items(list_id=b.id, items=["Колодки"])
    db_session.commit()
    ia_id = ia.id

    from langchain_core.messages import AIMessage as _AI

    def _planner(tms):
        if not tms:
            return _AI(content="", tool_calls=[
                {"name": "get_checklist", "args": {"mode": "items", "name": "кино"}, "id": "g1"},
                {"name": "get_checklist", "args": {"mode": "items", "name": "машина"}, "id": "g2"},
            ])
        done = {m.tool_call_id for m in tms}
        if "g3" not in done:
            import re as _re
            kino_rid = ""
            for m in tms:
                c = str(m.content)
                if "Кино к просмотру" in c and "result_type=items" in c:
                    mm = _re.search(r"result_id=(\S+)", c)
                    kino_rid = mm.group(1) if mm else ""
            return _AI(content="", tool_calls=[{
                "name": "mark_checklist_item_done",
                "args": {"item_id": ia_id, "source_result_id": kino_rid}, "id": "g3",
            }])
        return _AI(content="Отметила.")

    stub = _DynamicStubLLM(_planner)
    await _turn(db_session, u, stub, "что в кино и машина, отметь первый из кино", "213b-m5-2")
    tm = _tool_message_for(stub, "g3")
    assert tm is not None and "ok:done" in str(tm.content), tm.content
    db_session.expire_all()
    from sreda.db.models.checklists import ChecklistItem
    assert db_session.get(ChecklistItem, ia_id).status == "done"


# --- Срез C M9: наблюдаемость метрик в трейс -------------------------------


@pytest.mark.asyncio
async def test_m9_result_kind_marks(db_session, monkeypatch):
    """M9: checklist-события несут различимый result_kind в artifact (для метрик канарейки):
    mode_mismatch / name_required / ambiguous / checklist_redirect."""
    _flags(monkeypatch, unified=True, querykind=True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)
    svc = ChecklistService(db_session)
    svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Кино детям")  # для ambiguous
    db_session.commit()

    def _rk(stub, cid):
        m = _tool_message_for(stub, cid)
        return (getattr(m, "artifact", None) or {}).get("result_kind") if m else None

    # редирект: no-name items-вызов при high-conf «кино»... но теперь 2 «кино» → ambiguous.
    # Возьмём чистый редирект на однозначном «поход».
    svc2 = ChecklistService(db_session)
    ph = svc2.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Поход")
    svc2.add_items(list_id=ph.id, items=["Палатка"])
    db_session.commit()

    stub = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "get_checklist", "args": {"mode": "items"}, "id": "rd",
        }]),
        AIMessage(content="Вот."),
    ])
    await _turn(db_session, u, stub, "что в списке поход", "213b-m9-rd")
    assert _rk(stub, "rd") == "checklist_redirect", _rk(stub, "rd")

    # mode_mismatch
    stub2 = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "get_checklist", "args": {"mode": "overview"}, "id": "mm",
        }]),
        AIMessage(content="Вот."),
    ])
    await _turn(db_session, u, stub2, "что в списке поход", "213b-m9-mm")
    assert _rk(stub2, "mm") == "mode_mismatch", _rk(stub2, "mm")

    # ambiguous (два «кино»)
    stub3 = _RecordingStubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "get_checklist", "args": {"mode": "items", "name": "кино"}, "id": "am",
        }]),
        AIMessage(content="Уточни."),
    ])
    await _turn(db_session, u, stub3, "покажи список кино", "213b-m9-am")
    assert _rk(stub3, "am") == "ambiguous", _rk(stub3, "am")
