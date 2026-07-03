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
