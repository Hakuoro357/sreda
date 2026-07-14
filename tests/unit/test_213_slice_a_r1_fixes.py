"""#213 Срез A — тесты фикс-раунда R1 ревью (Codex high+medium+Claude).

Пинят: schema-паспорт envelope (п.10), composite-guard резолвера, internal-alias
через ranked (п.17), origin-гейт search-envelope, флаг-условную директиву
предслоя, зеркальную канонизацию при OFF (rollback п.20), recovery-исход (а)
чеклиста п.2, krai-кейсы резолвера (чужой tenant / archived id).
"""

from __future__ import annotations

import re as _re_mod

import pytest

from langchain_core.messages import AIMessage

from sreda.runtime import react_loop
from sreda.services.checklists import ChecklistService
from tests.unit.conftest import seed_telegram_user
from tests.unit.test_213_get_checklist_slice_a import (
    _RecordingStubLLM,
    _flag_unified,
    _housewife_tools,
    _mk_lists,
    _seed_kino,
    _tool_message_for,
)


def _parse_passport(out: str) -> dict:
    """Первая строка результата → key-value dict (schema-контракт п.10)."""
    head = out.splitlines()[0]
    d = {}
    for m in _re_mod.finditer(r"(\w+)=(\"([^\"]*)\"|\S+)", head):
        d[m.group(1)] = m.group(3) if m.group(3) is not None else m.group(2)
    return d


def test_passport_schema_items_ok(db_session, monkeypatch):
    """п.10: обязательные поля items-паспорта; overview-полей (count) нет."""
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)
    gc = _housewife_tools(db_session, u)["get_checklist"]

    p = _parse_passport(gc.invoke({"mode": "items", "name": "кино"}))
    assert p.get("result_type") == "items"
    assert p.get("result_id", "").startswith("r")
    assert p.get("checklist_name") == "Кино к просмотру"
    assert p.get("checklist_id", "").startswith("checklist_")
    assert p.get("matched_by") in ("exact", "fuzzy")
    assert p.get("resolution_status") in ("exact", "unique_fuzzy")
    assert "count" not in p, "items-паспорт не несёт overview-полей"


def test_passport_schema_overview_and_states(db_session, monkeypatch):
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)
    gc = _housewife_tools(db_session, u)["get_checklist"]

    p = _parse_passport(gc.invoke({"mode": "overview"}))
    assert p.get("result_type") == "overview" and "count" in p
    assert "checklist_name" not in p and "matched_by" not in p

    p = _parse_passport(gc.invoke({"mode": "items", "name": "сталоне"}))
    assert p.get("result_type") == "items" and p.get("resolution_status") == "not_found"

    svc = ChecklistService(db_session)
    svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Кино детям")
    db_session.commit()
    p = _parse_passport(gc.invoke({"mode": "items", "name": "кино"}))
    assert p.get("resolution_status") == "ambiguous" and "candidates" in p


def test_resolver_composite_needle_never_unique(db_session):
    """R1 high: «кино и машина» НЕ unique_fuzzy (reverse-substring выбирал «Машину»)."""
    u = seed_telegram_user(db_session)
    svc, (a, b) = _mk_lists(db_session, u, "Кино к просмотру", "Машина")
    res = svc.resolve_list_by_title_ranked(
        tenant_id=u.tenant_id, user_id=u.user_id, needle="кино и машина")
    assert res.status == "ambiguous", res.status
    assert {c.id for c in res.candidates} == {a.id, b.id}

    res2 = svc.resolve_list_by_title_ranked(
        tenant_id=u.tenant_id, user_id=u.user_id, needle="сталоне и рэмбо")
    assert res2.status == "not_found"


def test_resolver_composite_exact_name_wins(db_session):
    """Составное ИМЯ списка («Дела и покупки») — exact-ветка держит."""
    u = seed_telegram_user(db_session)
    svc, (a,) = _mk_lists(db_session, u, "Дела и покупки")
    res = svc.resolve_list_by_title_ranked(
        tenant_id=u.tenant_id, user_id=u.user_id, needle="дела и покупки")
    assert res.status == "exact" and res.checklist.id == a.id


def test_resolver_cross_tenant_and_archived_id(db_session):
    """Krai-кейсы матрицы: чужой tenant id и archived id → not_found."""
    u = seed_telegram_user(db_session)
    other = seed_telegram_user(
        db_session, chat_id="999888777", tenant_id="tenant_2", user_id="user_2",
        profile_id="tup_other")
    svc = ChecklistService(db_session)
    foreign = svc.create_list(tenant_id=other.tenant_id, user_id=other.user_id, title="Чужой")
    mine = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Архивный")
    svc.archive_list(tenant_id=u.tenant_id, user_id=u.user_id, list_id=mine.id)
    db_session.commit()

    res = svc.resolve_list_by_title_ranked(
        tenant_id=u.tenant_id, user_id=u.user_id, needle=foreign.id)
    assert res.status == "not_found"
    res = svc.resolve_list_by_title_ranked(
        tenant_id=u.tenant_id, user_id=u.user_id, needle=mine.id)
    # осознанно (decision log slice-A R1): архив скрыт и по id — отличие от легаси
    assert res.status == "not_found"


def test_internal_alias_show_checklist_on_uses_ranked_top1_legacy_form(db_session, monkeypatch):
    """п.17: internal show_checklist при ON — ranked внутри, наружу СТАРАЯ форма;
    ambiguous осознанно → top-1 (самый свежий)."""
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    svc = ChecklistService(db_session)
    a = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Кино к просмотру")
    svc.add_items(list_id=a.id, items=["Дюна"])
    b = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Кино детям")
    svc.add_items(list_id=b.id, items=["Тачки"])
    db_session.commit()

    sc = _housewife_tools(db_session, u)["show_checklist"]
    out = sc.invoke({"list_id_or_title": "кино"})  # internal-origin: без react-ctx
    assert out.startswith("# "), out  # старая форма
    assert "result_type" not in out
    assert "Кино детям" in out.splitlines()[0]  # top-1 = самый свежий (b позже)


def test_search_envelope_internal_origin_gets_legacy_form(db_session, monkeypatch):
    """R1 Claude MAJOR-2: internal list_checklist_items при ON — старая форма
    (иначе parse_list_checklist_items → ToolOutputContractViolation)."""
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)
    lci = _housewife_tools(db_session, u)["list_checklist_items"]

    out = lci.invoke({"title_match": "Скорпион"})  # internal: react-ctx не связан
    assert "result_type" not in out, out
    assert out.splitlines()[0].startswith("[clitem_"), out

    out_empty = lci.invoke({"title_match": "нет-такого"})
    assert out_empty == "empty"


@pytest.mark.asyncio
async def test_search_envelope_react_origin_on(db_session, monkeypatch):
    """LLM-origin (react tool-node) при ON — search-паспорт присутствует."""
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)

    scripted = [
        AIMessage(content="", tool_calls=[{
            "name": "list_checklist_items", "args": {"title_match": "Скорпион"},
            "id": "call_srch",
        }]),
        AIMessage(content="Нашла."),
    ]
    stub = _RecordingStubLLM(scripted)
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="213-srch-1", llm=stub, user_text="найди пункт скорпион",
        inbound_message_id="213-srch-1-msg", channel="react",
    )
    tm = _tool_message_for(stub, "call_srch")
    assert tm is not None
    assert "result_type=search" in str(tm.content), tm.content


def test_preflight_directive_unified_no_legacy_name(monkeypatch):
    """R1 Claude MAJOR-1: директива предслоя при ON не велит звать list_checklists."""
    import sreda.config.settings as sm
    from sreda.runtime import react_preflight as rp

    monkeypatch.setenv("SREDA_CHECKLIST_UNIFIED", "1")
    sm.get_settings.cache_clear()
    hint_on = rp._section_hint("покажи список кино")
    assert hint_on is not None
    assert "Используй get_checklist" in hint_on, hint_on
    assert "Используй list_checklists" not in hint_on, hint_on
    assert rp._directive_for("checklists") == hint_on

    monkeypatch.setenv("SREDA_CHECKLIST_UNIFIED", "0")
    sm.get_settings.cache_clear()
    hint_off = rp._section_hint("покажи список кино")
    # #374: legacy-директива теперь РАЗЛИЧАЕТ конкретный список от обзора (раньше велела
    # list_checklists ВСЕГДА → «покажи список X» давал обзор всех вместо пунктов X, ~50% промахов).
    assert "show_checklist" in hint_off      # назван конкретный → его пункты
    assert "list_checklists" in hint_off     # «какие списки» → обзор
    assert "get_checklist" not in hint_off   # legacy НЕ упоминает unified-инструмент


@pytest.mark.asyncio
async def test_rollback_off_canonicalizes_get_checklist_to_legacy(db_session, monkeypatch):
    """п.20 (rollback) + R1 Claude MAJOR-4: при OFF праймленный историей вызов
    get_checklist зеркально канонизируется в легаси — не family_not_loaded-петля."""
    _flag_unified(monkeypatch, False)
    u = seed_telegram_user(db_session)
    cl = _seed_kino(db_session, u)

    scripted = [
        AIMessage(content="", tool_calls=[
            {"name": "get_checklist", "args": {"mode": "items", "name": "кино"}, "id": "call_rb1"},
            {"name": "get_checklist", "args": {"mode": "overview"}, "id": "call_rb2"},
        ]),
        AIMessage(content="Вот."),
    ]
    stub = _RecordingStubLLM(scripted)
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="213-rb-1", llm=stub, user_text="покажи список кино",
        inbound_message_id="213-rb-1-msg", channel="react",
    )
    rb1 = _tool_message_for(stub, "call_rb1")
    rb2 = _tool_message_for(stub, "call_rb2")
    assert rb1 is not None and rb2 is not None
    assert str(rb1.content).startswith(f"# Кино к просмотру ({cl.id})"), rb1.content
    assert "need_family" not in str(rb1.content) and "недоступен" not in str(rb1.content)
    assert "Кино к просмотру" in str(rb2.content)  # легаси-форма list_checklists
    assert "result_type" not in str(rb2.content)


@pytest.mark.asyncio
async def test_recovery_second_call_with_name_executes(db_session, monkeypatch):
    """п.2 recovery, исход (а): после name_required модель перезывает С именем — исполнено."""
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)

    scripted = [
        AIMessage(content="", tool_calls=[{
            "name": "get_checklist", "args": {"mode": "items"}, "id": "call_r1",
        }]),
        AIMessage(content="", tool_calls=[{
            "name": "get_checklist", "args": {"mode": "items", "name": "кино"}, "id": "call_r2",
        }]),
        AIMessage(content="Вот список кино."),
    ]
    stub = _RecordingStubLLM(scripted)
    reply = await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="213-rec-1", llm=stub, user_text="покажи список кино",
        inbound_message_id="213-rec-1-msg", channel="react",
    )
    first = _tool_message_for(stub, "call_r1")
    second = _tool_message_for(stub, "call_r2")
    assert first is not None and "name_required" in str(first.content)
    assert second is not None and "result_type=items" in str(second.content)
    assert "Скорпион" in str(second.content)
    assert reply
