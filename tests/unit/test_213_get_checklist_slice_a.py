"""#213 Срез A - RED-контракт унификации get_checklist(mode, name).

Пинит чеклист приёмки (vex-assistant plans/213-acceptance-draft.md):
ядро пп.1 (инцидент), 2 (recovery/структурные отказы), 4 (ambiguous →
candidates), 6 (канонизация LLM-origin), 7 (OFF = легаси);
инж.блок пп.10 (envelope), 16 (депрекейт-экспозиция), 17 (internal-alias
policy), 18 (порядок канонизации), 22 (exact-id ветка).

КРАСНЫЙ до реализации Среза A. НЕ удалять - контракт ядра
plans/213-cycle-final.md. Формат envelope зафиксирован здесь:
первая строка результата - паспорт
``result_type=<items|overview|search> result_id=<id> ...``,
дальше тело в прежнем построчном формате.
"""

from __future__ import annotations

import pytest

from langchain_core.messages import AIMessage

from sreda.runtime import react_loop
from sreda.services.checklists import ChecklistService
from tests.unit.conftest import seed_telegram_user


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------


def _flag_unified(monkeypatch, on: bool) -> None:
    """SREDA_CHECKLIST_UNIFIED через env (как канон #197/#221 тестов)."""
    import sreda.config.settings as sm

    monkeypatch.setenv("SREDA_CHECKLIST_UNIFIED", "1" if on else "0")
    sm.get_settings.cache_clear()


def _seed_kino(db_session, u, items=("Скорпион", "Машина войны", "Апгрейд", "Джентльмены")):
    svc = ChecklistService(db_session)
    cl = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Кино к просмотру")
    svc.add_items(list_id=cl.id, items=list(items))
    db_session.commit()
    return cl


def _housewife_tools(db_session, u):
    from sreda.services.housewife_chat_tools import build_housewife_tools

    class _NoEmb:
        def embed_query(self, *_a, **_k):  # noqa: ANN002, ANN003
            return [0.0]

    tools = build_housewife_tools(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        pending_buttons_state=None, menu_display_state=None,
        embedding_client=_NoEmb(),
    )
    return {t.name: t for t in tools}


class _RecordingStubLLM:
    """Скриптованный LLM: запоминает набор инструментов каждого bind_tools
    и ВСЕ входящие messages каждого invoke (для проверки ToolMessage)."""

    def __init__(self, scripted: list[AIMessage]) -> None:
        self._scripted = scripted
        self._i = 0
        self.binds: list[set[str]] = []
        self.seen_messages: list[list] = []

    def bind_tools(self, tools):  # noqa: ANN001
        self.binds.append({getattr(t, "name", None) for t in tools})
        return self

    def invoke(self, messages):  # noqa: ANN001
        self.seen_messages.append(list(messages))
        msg = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return msg


def _tool_message_for(stub: _RecordingStubLLM, call_id: str):
    """Найти ToolMessage с данным tool_call_id среди увиденных стабом сообщений."""
    for batch in stub.seen_messages:
        for m in batch:
            if getattr(m, "tool_call_id", None) == call_id:
                return m
    return None


# ---------------------------------------------------------------------------
# A. Схема инструмента get_checklist (флаг ON)
# ---------------------------------------------------------------------------


def test_incident_items_by_name(db_session, monkeypatch):
    """Приёмка п.1: «покажи список кино» → items ИМЕННО этого списка, не обзор."""
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)
    gc = _housewife_tools(db_session, u)["get_checklist"]

    out = gc.invoke({"mode": "items", "name": "кино"})

    first_line = out.splitlines()[0]
    assert "result_type=items" in first_line, out
    assert "result_id=" in first_line, out
    assert "Кино к просмотру" in out
    for title in ("Скорпион", "Машина войны", "Апгрейд", "Джентльмены"):
        assert title in out, f"пункт {title!r} не показан: {out}"


def test_items_without_name_structural_refusal(db_session, monkeypatch):
    """Приёмка п.2: mode=items без name → структурный name_required, НЕ обзор."""
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)
    svc = ChecklistService(db_session)
    svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Поход")
    db_session.commit()
    gc = _housewife_tools(db_session, u)["get_checklist"]

    for args in ({"mode": "items"}, {"mode": "items", "name": ""}, {"mode": "items", "name": "  "}):
        out = gc.invoke(args)
        assert "name_required" in out, out
        # главный инвариант класса #213: отказ НЕ несёт конкурирующий обзор
        assert "Поход" not in out and "Кино к просмотру" not in out, (
            f"name_required не должен перечислять списки: {out}")


def test_overview_with_name_structural_error(db_session, monkeypatch):
    """Схема: name при mode=overview запрещён (хедж внутри валидной схемы невозможен)."""
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)
    gc = _housewife_tools(db_session, u)["get_checklist"]

    out = gc.invoke({"mode": "overview", "name": "кино"})
    assert "name_forbidden" in out, out
    assert "Скорпион" not in out


def test_overview_lists_all(db_session, monkeypatch):
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)
    svc = ChecklistService(db_session)
    cl2 = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Поход")
    svc.add_items(list_id=cl2.id, items=["Палатка"])
    db_session.commit()
    gc = _housewife_tools(db_session, u)["get_checklist"]

    out = gc.invoke({"mode": "overview"})
    first_line = out.splitlines()[0]
    assert "result_type=overview" in first_line, out
    assert "Кино к просмотру" in out and "Поход" in out
    # обзор НЕ раскрывает пункты (это не items)
    assert "Скорпион" not in out and "Палатка" not in out


def test_invalid_mode_rejected(db_session, monkeypatch):
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    gc = _housewife_tools(db_session, u)["get_checklist"]

    out = gc.invoke({"mode": "search"})
    assert "invalid_mode" in out, out


def test_ambiguous_returns_candidates(db_session, monkeypatch):
    """Приёмка п.4: два пересекающихся имени → candidates, НЕ молчаливый top-1."""
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    svc = ChecklistService(db_session)
    # оба — fuzzy-матчи на «кино», точного имени «кино» нет → честная неоднозначность
    a = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Кино к просмотру")
    svc.add_items(list_id=a.id, items=["Дюна"])
    b = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Кино детям")
    svc.add_items(list_id=b.id, items=["Тачки"])
    db_session.commit()
    gc = _housewife_tools(db_session, u)["get_checklist"]

    out = gc.invoke({"mode": "items", "name": "кино"})
    assert "resolution=ambiguous" in out, out
    assert "Кино к просмотру" in out and "Кино детям" in out  # кандидаты названы
    assert "Дюна" not in out and "Тачки" not in out  # пункты НЕ раскрыты


def test_exact_id_resolution(db_session, monkeypatch):
    """Приёмка п.22 (deferred MINOR R5): checklist_-id → resolution=exact."""
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    cl = _seed_kino(db_session, u)
    gc = _housewife_tools(db_session, u)["get_checklist"]

    out = gc.invoke({"mode": "items", "name": cl.id})
    assert "resolution=exact" in out, out
    assert "Скорпион" in out


def test_not_found_no_competing_names(db_session, monkeypatch):
    """not_found НЕ перечисляет имена других списков (анти-класс #213)."""
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)
    gc = _housewife_tools(db_session, u)["get_checklist"]

    out = gc.invoke({"mode": "items", "name": "сталоне"})
    assert "resolution=not_found" in out, out
    assert "Кино к просмотру" not in out


def test_items_envelope_no_overview_fields(db_session, monkeypatch):
    """Приёмка п.10: items-результат не содержит строк других списков/глобальных счётчиков."""
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)
    svc = ChecklistService(db_session)
    cl2 = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Поход")
    svc.add_items(list_id=cl2.id, items=["Палатка"])
    db_session.commit()
    gc = _housewife_tools(db_session, u)["get_checklist"]

    out = gc.invoke({"mode": "items", "name": "кино"})
    assert "Поход" not in out and "Палатка" not in out, out


# ---------------------------------------------------------------------------
# B. Ranked-резолвер (сервис)
# ---------------------------------------------------------------------------


def _mk_lists(db_session, u, *titles):
    svc = ChecklistService(db_session)
    made = [svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title=t) for t in titles]
    db_session.commit()
    return svc, made


def test_resolver_exact_title(db_session):
    u = seed_telegram_user(db_session)
    svc, (a, b) = _mk_lists(db_session, u, "Кино", "Кино детям")
    res = svc.resolve_list_by_title_ranked(tenant_id=u.tenant_id, user_id=u.user_id, needle="Кино")
    assert res.status == "exact"
    assert res.checklist.id == a.id


def test_resolver_unique_fuzzy(db_session):
    u = seed_telegram_user(db_session)
    svc, (a,) = _mk_lists(db_session, u, "Кино к просмотру")
    res = svc.resolve_list_by_title_ranked(tenant_id=u.tenant_id, user_id=u.user_id, needle="кино")
    assert res.status == "unique_fuzzy"
    assert res.checklist.id == a.id


def test_resolver_ambiguous(db_session):
    u = seed_telegram_user(db_session)
    svc, (a, b) = _mk_lists(db_session, u, "Кино к просмотру", "Кино детям")
    res = svc.resolve_list_by_title_ranked(tenant_id=u.tenant_id, user_id=u.user_id, needle="кино")
    assert res.status == "ambiguous"
    got = {c.id for c in res.candidates}
    assert got == {a.id, b.id}


def test_resolver_not_found(db_session):
    u = seed_telegram_user(db_session)
    svc, _ = _mk_lists(db_session, u, "Кино")
    res = svc.resolve_list_by_title_ranked(tenant_id=u.tenant_id, user_id=u.user_id, needle="сталоне")
    assert res.status == "not_found"
    assert res.checklist is None


def test_resolver_id_exact(db_session):
    u = seed_telegram_user(db_session)
    svc, (a,) = _mk_lists(db_session, u, "Кино")
    res = svc.resolve_list_by_title_ranked(tenant_id=u.tenant_id, user_id=u.user_id, needle=a.id)
    assert res.status == "exact"
    assert res.checklist.id == a.id


# ---------------------------------------------------------------------------
# C. Реестр / экспозиция по флагу
# ---------------------------------------------------------------------------


def test_registry_agreement():
    """get_checklist прошит: manifest (checklists) + op-class (read_pure) + алиасы валидны."""
    from sreda.services.tool_schemas.families import (
        DEPRECATED_TOOL_ALIASES, TOOL_FAMILY_MANIFEST, TOOL_OP_CLASS,
    )

    assert TOOL_FAMILY_MANIFEST.get("get_checklist") == "checklists"
    assert TOOL_OP_CLASS.get("get_checklist") == "read_pure"
    assert DEPRECATED_TOOL_ALIASES == {
        "list_checklists": "get_checklist",
        "show_checklist": "get_checklist",
    }
    for target in DEPRECATED_TOOL_ALIASES.values():
        assert target in TOOL_FAMILY_MANIFEST


@pytest.mark.asyncio
async def test_flag_off_legacy_toolset(db_session, monkeypatch):
    """Приёмка п.7 (OFF = легаси): у LLM старая пара, get_checklist НЕ экспонирован."""
    _flag_unified(monkeypatch, False)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)

    stub = _RecordingStubLLM([AIMessage(content="Привет.")])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="213-off-1", llm=stub, user_text="покажи список кино",
        inbound_message_id="213-off-1-msg", channel="react",
    )
    assert stub.binds, "bind_tools не вызывался"
    exposed = set().union(*stub.binds)
    assert "show_checklist" in exposed and "list_checklists" in exposed
    assert "get_checklist" not in exposed


@pytest.mark.asyncio
async def test_flag_on_unified_toolset(db_session, monkeypatch):
    """Приёмка п.16: при ON у LLM get_checklist, старой пары НЕТ в экспозиции."""
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)

    stub = _RecordingStubLLM([AIMessage(content="Привет.")])
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="213-on-1", llm=stub, user_text="покажи список кино",
        inbound_message_id="213-on-1-msg", channel="react",
    )
    assert stub.binds, "bind_tools не вызывался"
    exposed = set().union(*stub.binds)
    assert "get_checklist" in exposed
    assert "show_checklist" not in exposed and "list_checklists" not in exposed


# ---------------------------------------------------------------------------
# D. Канонизация LLM-origin старых имён (флаг ON) + легаси при OFF
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_origin_show_checklist_canonicalized(db_session, monkeypatch):
    """Приёмка п.6/п.18: LLM-origin show_checklist при ON → НОВЫЙ механизм (envelope),
    не старая форма, не unavailable-петля."""
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)

    scripted = [
        AIMessage(content="", tool_calls=[{
            "name": "show_checklist", "args": {"list_id_or_title": "кино"}, "id": "call_canon",
        }]),
        AIMessage(content="Вот твой список."),
    ]
    stub = _RecordingStubLLM(scripted)
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="213-canon-1", llm=stub, user_text="покажи список кино",
        inbound_message_id="213-canon-1-msg", channel="react",
    )
    tm = _tool_message_for(stub, "call_canon")
    assert tm is not None, "ToolMessage на канонизированный вызов не дошёл до модели"
    content = str(tm.content)
    assert "result_type=items" in content, content
    assert "Скорпион" in content
    assert "недоступен" not in content and "need_family" not in content, (
        f"канонизация должна срабатывать ДО unavailable-ветки: {content}")


@pytest.mark.asyncio
async def test_llm_origin_list_checklists_canonicalized(db_session, monkeypatch):
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)

    scripted = [
        AIMessage(content="", tool_calls=[{
            "name": "list_checklists", "args": {}, "id": "call_canon2",
        }]),
        AIMessage(content="Вот списки."),
    ]
    stub = _RecordingStubLLM(scripted)
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="213-canon-2", llm=stub, user_text="какие у меня списки",
        inbound_message_id="213-canon-2-msg", channel="react",
    )
    tm = _tool_message_for(stub, "call_canon2")
    assert tm is not None
    content = str(tm.content)
    assert "result_type=overview" in content, content
    assert "Кино к просмотру" in content


@pytest.mark.asyncio
async def test_llm_origin_ambiguous_via_alias_returns_candidates(db_session, monkeypatch):
    """Приёмка п.6 (red-кейс R5): алиас при ON + неоднозначный needle → candidates, НЕ top-1."""
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    svc = ChecklistService(db_session)
    a = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Кино к просмотру")
    svc.add_items(list_id=a.id, items=["Дюна"])
    b = svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Кино детям")
    svc.add_items(list_id=b.id, items=["Тачки"])
    db_session.commit()

    scripted = [
        AIMessage(content="", tool_calls=[{
            "name": "show_checklist", "args": {"list_id_or_title": "кино"}, "id": "call_amb",
        }]),
        AIMessage(content="Уточни, пожалуйста."),
    ]
    stub = _RecordingStubLLM(scripted)
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="213-canon-3", llm=stub, user_text="покажи список кино",
        inbound_message_id="213-canon-3-msg", channel="react",
    )
    tm = _tool_message_for(stub, "call_amb")
    assert tm is not None
    content = str(tm.content)
    assert "resolution=ambiguous" in content, content
    assert "Дюна" not in content and "Тачки" not in content, (
        f"молчаливый top-1 через алиас при ON запрещён: {content}")


@pytest.mark.asyncio
async def test_flag_off_show_checklist_legacy_format(db_session, monkeypatch):
    """Приёмка п.7: OFF → старая форма show_checklist байт-в-байт (без envelope)."""
    _flag_unified(monkeypatch, False)
    u = seed_telegram_user(db_session)
    cl = _seed_kino(db_session, u)

    scripted = [
        AIMessage(content="", tool_calls=[{
            "name": "show_checklist", "args": {"list_id_or_title": "кино"}, "id": "call_leg",
        }]),
        AIMessage(content="Вот."),
    ]
    stub = _RecordingStubLLM(scripted)
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="213-leg-1", llm=stub, user_text="покажи список кино",
        inbound_message_id="213-leg-1-msg", channel="react",
    )
    tm = _tool_message_for(stub, "call_leg")
    assert tm is not None
    content = str(tm.content)
    assert content.startswith(f"# Кино к просмотру ({cl.id})"), content
    assert "result_type" not in content, f"envelope при OFF запрещён (легаси): {content}"


# ---------------------------------------------------------------------------
# E. Хедж на схеме (ON): no-name вызов не создаёт конкурирующий результат
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hedge_batch_no_name_gets_refusal_not_overview(db_session, monkeypatch):
    """Инцидент-паттерн: батч [named, no-name] → no-name даёт name_required,
    в контексте НЕТ второго списочного результата."""
    _flag_unified(monkeypatch, True)
    u = seed_telegram_user(db_session)
    _seed_kino(db_session, u)
    svc = ChecklistService(db_session)
    svc.create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Поход")
    db_session.commit()

    scripted = [
        AIMessage(content="", tool_calls=[
            {"name": "get_checklist", "args": {"mode": "items", "name": "кино"}, "id": "call_named"},
            {"name": "get_checklist", "args": {"mode": "items"}, "id": "call_noname"},
        ]),
        AIMessage(content="Вот твой список кино."),
    ]
    stub = _RecordingStubLLM(scripted)
    await react_loop.handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id="213-hedge-1", llm=stub, user_text="покажи список кино",
        inbound_message_id="213-hedge-1-msg", channel="react",
    )
    named = _tool_message_for(stub, "call_named")
    noname = _tool_message_for(stub, "call_noname")
    assert named is not None and noname is not None
    assert "result_type=items" in str(named.content)
    assert "name_required" in str(noname.content), noname.content
    assert "Поход" not in str(noname.content), (
        f"no-name отказ не должен нести обзор (класс #213): {noname.content}")
