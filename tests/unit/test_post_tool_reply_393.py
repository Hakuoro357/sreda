"""#393 — филлер вместо отчёта о сделанном на пост-tool проходах («сделала, но сказала не то»).

Класс #376. На ходу с УСПЕШНЫМ мутирующим act финальная реплика ОБЯЗАНА называть результат.
Решение владельца — **вариант C**:
  1) grounding_note (часть 1, ПРОМПТ узла chat) — ТОЛЬКО серверные факты результата (статус/кол-во/
     тип), БЕЗ сырых имён → инъекц-surface закрыт структурно (ни одна юзер-строка не в инструкциях);
  2) страховка (часть 2, финализация handle_turn) — если реплика не называет результат ИЛИ несёт
     машинную утечку, детерминированная тёплая подмена; имена — из РЕЗУЛЬТАТА инструмента, только в
     ВЫВОДЕ юзеру (не инъекц-поверхность); при неотображаемом имени — грациозная деградация к фактам.
Фикс PATH-AGNOSTIC (легаси + unified).

Прод-репро (tenant_max_40921122, 18.07, SGR OFF, guard_scope=legacy):
  A. «Добавь в список Дача грабли, лопату и перчатки» → add_checklist_items ok → «приняла к сведению».
  B. «Удали список Дача» → confirm → «да» → archive_checklist ok → дамп чужих списков.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from sreda.runtime.react_loop import handle_turn
from sreda.runtime.react_result_report import (
    collect_successful_writes,
    fallback_reply,
    grounding_note,
    reply_grounds_result,
    reply_has_tech_leak,
)
from sreda.services.checklists import ChecklistService
from tests.unit.conftest import seed_telegram_user


# ─────────────────────────── синтетика для чистых функций ───────────────────────────

def _ai_call(name: str, args: dict, cid: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


def _tm(content: str, *, name: str = "add_checklist_items", cid: str = "c1",
        kind: str | None = "ok") -> ToolMessage:
    art = {"result_kind": kind} if kind else None
    return ToolMessage(content=content, name=name, tool_call_id=cid, artifact=art)


def _okv2_add(created: list[str]) -> str:
    import json
    return "okv2:added:" + json.dumps(
        {"added_count": len(created), "duplicate_count": 0,
         "checklist_id": "checklist_" + "a" * 24, "created": created},
        ensure_ascii=False, separators=(",", ":"))


_OKV2_ADD = _okv2_add(["грабли", "лопату", "перчатки"])


def _add_msgs() -> list:
    return [
        HumanMessage(content="Добавь в список Дача грабли, лопату и перчатки"),
        _ai_call("add_checklist_items",
                 {"list_id_or_title": "Дача", "items": ["грабли", "лопату", "перчатки"]}),
        _tm(_OKV2_ADD),
    ]


def _archive_msgs() -> list:
    return [
        HumanMessage(content="Удали список Дача"),
        _ai_call("archive_checklist", {"list_id_or_title": "Дача"}),
        _tm("ok:archived:checklist_" + "a" * 24, name="archive_checklist"),
    ]


# ─────────────────────────── collect_successful_writes ───────────────────────────

def test_collect_add_names_list_and_items_393():
    acts = collect_successful_writes(_add_msgs())
    assert len(acts) == 1
    assert acts[0].target == "Дача" and acts[0].count == 3
    assert set(acts[0].items) >= {"грабли", "лопату", "перчатки"}


def test_collect_archive_names_target_393():
    acts = collect_successful_writes(_archive_msgs())
    assert len(acts) == 1 and acts[0].target == "Дача" and acts[0].items == ()


def test_collect_excludes_confirm_decline_393():
    """МУТ-guard: confirm-ОТКАЗ «Хорошо, не трогаю.» — НЕ startswith ok:archived → не успех (иначе
    отменённый archive → ложный успех, хуже филлера)."""
    msgs = [HumanMessage(content="Удали список Дача"),
            _ai_call("archive_checklist", {"list_id_or_title": "Дача"}),
            _tm("Хорошо, не трогаю.", name="archive_checklist")]
    assert collect_successful_writes(msgs) == ()


def test_collect_excludes_error_result_393():
    msgs = [HumanMessage(content="Удали список Дача"),
            _ai_call("archive_checklist", {"list_id_or_title": "Дача"}),
            _tm("error:not_found: 'Дача'", name="archive_checklist")]
    assert collect_successful_writes(msgs) == ()


def test_collect_excludes_pure_read_393():
    msgs = [HumanMessage(content="какие у меня списки"),
            _ai_call("list_checklists", {}),
            _tm("Кино; Поход", name="list_checklists")]
    assert collect_successful_writes(msgs) == ()


def test_collect_excludes_add_all_dup_393():
    """all-dup (okv2 created=[]) → нет эффекта → не заземляем (иначе ложный успех)."""
    dup = ('okv2:added_with_dups:{"added_count":0,"duplicate_count":3,"checklist_id":"checklist_'
           + "a" * 24 + '","created":[],"duplicates_existing":["грабли"]}')
    msgs = [HumanMessage(content="добавь"),
            _ai_call("add_checklist_items", {"list_id_or_title": "Дача", "items": ["грабли"]}),
            _tm(dup)]
    assert collect_successful_writes(msgs) == ()


def test_collect_excludes_create_and_generic_393():
    """create_checklist (возвращает существующий — не отличить) и незнакомый write (save_recipe) —
    вне allowlist → не заземляем."""
    m1 = [HumanMessage(content="создай список Дача"),
          _ai_call("create_checklist", {"title": "Дача"}),
          _tm("ok:created:checklist_" + "a" * 24 + ":Дача", name="create_checklist")]
    m2 = [HumanMessage(content="сохрани борщ"),
          _ai_call("save_recipe", {"title": "Борщ"}),
          _tm('okv2:duplicate:{"recipe_id":"rec_' + "a" * 24 + '","title":"Борщ"}', name="save_recipe")]
    assert collect_successful_writes(m1) == () and collect_successful_writes(m2) == ()


def test_collect_scopes_to_current_turn_393():
    msgs = [*_add_msgs(), HumanMessage(content="спасибо")]
    assert collect_successful_writes(msgs) == ()


def test_collect_mixed_decline_and_success_393():
    """R4 (sol): смешанный набор — отклонённый confirm-акт (нет ok:) + УСПЕШНЫЙ add → collect берёт
    ТОЛЬКО успешный (declined исключён), чтобы отчёт об успехе не потерялся при отказе."""
    msgs = [HumanMessage(content="удали Поход и добавь в Дача грабли"),
            _ai_call("archive_checklist", {"list_id_or_title": "Поход"}, cid="c1"),
            _ai_call("add_checklist_items", {"list_id_or_title": "Дача", "items": ["грабли"]}, cid="c2"),
            _tm("Хорошо, не трогаю.", name="archive_checklist", cid="c1"),
            _tm(_okv2_add(["грабли"]), cid="c2")]
    acts = collect_successful_writes(msgs)
    assert len(acts) == 1 and acts[0].tool == "add_checklist_items" and acts[0].target == "Дача"


# ─── вариант C: неотображаемое имя НЕ роняет акт (несёт count/тип для грациозной деградации) ───

def test_collect_undisplayable_names_degrade_not_drop_393():
    """Вариант C + owner R4-c: латиница/id/тех-конструкция в имени → акт НЕ дропается, несёт count+тип;
    страховка деградирует к фактам (кол-во+тип), а не молчит сырым филлером."""
    a = collect_successful_writes([
        HumanMessage(content="добавь"),
        _ai_call("add_checklist_items", {"list_id_or_title": "Дача", "items": ["milk"]}),
        _tm(_okv2_add(["milk"]))])
    assert len(a) == 1 and a[0].target == "Дача" and a[0].items == () and a[0].count == 1
    assert "1 пункт" in fallback_reply(a) and "Дача" in fallback_reply(a)
    t = collect_successful_writes([
        HumanMessage(content="удали"),
        _ai_call("archive_checklist", {"list_id_or_title": "checklist_" + "a" * 24}),
        _tm("ok:archived:checklist_" + "a" * 24, name="archive_checklist")])
    assert len(t) == 1 and t[0].target == ""
    fb = fallback_reply(t)
    # #394: финал-копия на языке юзера («удалила список»), без внутреннего «архив*».
    assert fb and "checklist_" not in fb.lower() and "удалила список" in fb


def test_detector_and_fallback_undisplayable_items_use_count_393():
    """R5 (sol CRITICAL): часть/все добавленные пункты неотображаемы (len(items)<count) → НЕ заземлён
    (P0-филлер не остаётся для латиница/id-пунктов), страховка деградирует к КОЛИЧЕСТВУ (не теряет count)."""
    full = collect_successful_writes([
        HumanMessage(content="добавь"),
        _ai_call("add_shopping_items", {"items": ["iPhone"]}),
        _tm(_okv2_add(["iPhone"]), name="add_shopping_items")])
    assert len(full) == 1 and full[0].count == 1 and full[0].items == ()
    assert reply_grounds_result("Добавила, готово.", full) is False
    assert reply_grounds_result("Купила iPhone.", full) is False
    assert fallback_reply(full) == "Готово, добавила 1 пункт в список покупок."
    part = collect_successful_writes([
        HumanMessage(content="добавь"),
        _ai_call("add_shopping_items", {"items": ["молоко", "iPhone"]}),
        _tm(_okv2_add(["молоко", "iPhone"]), name="add_shopping_items")])
    assert len(part) == 1 and part[0].count == 2 and part[0].items == ("молоко",)
    assert reply_grounds_result("Добавила молоко.", part) is False
    assert fallback_reply(part) == "Готово, добавила 2 пункта в список покупок."


def test_collect_long_legit_name_graceful_truncate_393():
    """owner R4-c: длинное ЛЕГИТ имя (>порога) → graceful трункейт для показа, акт НЕ дропается."""
    long_name = "Дела на дачу этим летом обязательно успеть сделать до конца августа непременно всё"
    a = collect_successful_writes([
        HumanMessage(content="добавь"),
        _ai_call("add_checklist_items", {"list_id_or_title": long_name, "items": ["грабли"]}),
        _tm(_okv2_add(["грабли"]))])
    assert len(a) == 1 and a[0].target and len(a[0].target) <= 64
    assert long_name.startswith(a[0].target.rstrip())
    assert fallback_reply(a).startswith("Готово, добавила в список «Дела на дачу")


# ─────────────────────────── grounding_note — ТОЛЬКО серверные факты, БЕЗ имён (вариант C) ───────────────────────────

def test_grounding_note_has_facts_not_names_393():
    """Вариант C (ГЛАВНОЕ): note несёт статус+кол-во+тип, НО НИ ОДНОГО сырого имени → ни одна
    юзер-строка не входит в инструкции модели (инъекц-surface закрыт структурно)."""
    note = grounding_note(collect_successful_writes(_add_msgs()))
    assert "успешно выполнено" in note
    assert "3 пункта" in note and "чек-лист" in note
    low = note.lower()
    assert "дача" not in low
    assert "грабл" not in low and "лопат" not in low and "перчатк" not in low


def test_grounding_note_archive_facts_393():
    note = grounding_note(collect_successful_writes(_archive_msgs()))
    # #394: серверный факт заряжает голос на языке юзера («удалён список»), без «архив*».
    assert "удалён список" in note and "дача" not in note.lower()
    assert "архив" not in note.lower()


# ─────────────────────────── reply_grounds_result (детектор) ───────────────────────────

def test_detector_add_filler_not_grounded_393():
    assert reply_grounds_result("Хорошо, приняла к сведению.", collect_successful_writes(_add_msgs())) is False


def test_detector_add_full_report_grounded_393():
    acts = collect_successful_writes(_add_msgs())
    assert reply_grounds_result("Добавила в Дачу грабли, лопату и перчатки.", acts) is True
    assert reply_grounds_result("Записала грабли, лопата и перчатки в Дачу.", acts) is True


def test_detector_add_requires_list_and_all_items_393():
    """Полнота (sol+terra R2): назван 1 из 3 пунктов, или пункты без имени списка → не заземлён."""
    acts = collect_successful_writes(_add_msgs())
    assert reply_grounds_result("Добавила грабли.", acts) is False
    assert reply_grounds_result("Добавила грабли и лопату.", acts) is False
    assert reply_grounds_result("Добавила грабли, лопату и перчатки.", acts) is False


def test_detector_all_acts_required_393():
    msgs = [HumanMessage(content="добавь в дача грабли и удали Поход"),
            _ai_call("add_checklist_items", {"list_id_or_title": "Дача", "items": ["грабли"]}, cid="c1"),
            _ai_call("archive_checklist", {"list_id_or_title": "Поход"}, cid="c2"),
            _tm(_okv2_add(["грабли"]), cid="c1"),
            _tm("ok:archived:checklist_" + "b" * 24, name="archive_checklist", cid="c2")]
    acts = collect_successful_writes(msgs)
    assert len(acts) == 2
    assert reply_grounds_result("Добавила грабли в Дачу.", acts) is False
    assert reply_grounds_result("Добавила грабли в Дачу, убрала Поход.", acts) is True


def test_detector_archive_dump_vs_named_393():
    acts = collect_successful_writes(_archive_msgs())
    assert reply_grounds_result("Вот твои списки: Кино, Поход, Дела на сегодня.", acts) is False
    assert reply_grounds_result("Убрала список «Дача».", acts) is True


def test_detector_word_boundary_no_substring_false_match_393():
    """terra R1: stem «дел» списка «Дела» НЕ матчит «Сделала» (токены, не подстрока)."""
    msgs = [HumanMessage(content="удали Дела"),
            _ai_call("archive_checklist", {"list_id_or_title": "Дела"}),
            _tm("ok:archived:checklist_" + "a" * 24, name="archive_checklist")]
    acts = collect_successful_writes(msgs)
    assert reply_grounds_result("Сделала, готово.", acts) is False
    assert reply_grounds_result("Убрала «Дела».", acts) is True


def test_detector_short_noun_required_and_no_chai_chainik_393():
    """R3: короткие существительные (3 симв.) ОБЯЗАТЕЛЬНЫ. owner R4-d: «чай» НЕ матчит «чайник»."""
    a = collect_successful_writes([
        HumanMessage(content="добавь"),
        _ai_call("add_shopping_items", {"items": ["сыр и хлеб"]}),
        _tm(_okv2_add(["сыр и хлеб"]), name="add_shopping_items")])
    assert reply_grounds_result("Добавила хлеб.", a) is False
    assert reply_grounds_result("Добавила сыр и хлеб.", a) is True
    chai = collect_successful_writes([
        HumanMessage(content="добавь"),
        _ai_call("add_shopping_items", {"items": ["чай"]}),
        _tm(_okv2_add(["чай"]), name="add_shopping_items")])
    assert reply_grounds_result("Купила чайник.", chai) is False
    assert reply_grounds_result("Купила чай.", chai) is True


def test_detector_multiword_all_significant_tokens_393():
    a = collect_successful_writes([
        HumanMessage(content="добавь"),
        _ai_call("add_shopping_items", {"items": ["молоко без лактозы"]}),
        _tm(_okv2_add(["молоко без лактозы"]), name="add_shopping_items")])
    assert reply_grounds_result("Молоко записала.", a) is False
    assert reply_grounds_result("Записала молоко без лактозы.", a) is True


def test_detector_summarizing_voice_substituted_393():
    """owner R4-b (документируем границу): суммаризующий голос («добавила всё») НЕ называет пункты →
    заземления нет → сработает подмена (не сюрприз)."""
    assert reply_grounds_result("Добавила всё, что просили.", collect_successful_writes(_add_msgs())) is False


# ─────────────────────────── fallback_reply / чистота ───────────────────────────

def _assert_clean(txt: str) -> None:
    low = txt.lower()
    assert "—" not in txt and "checklist_" not in low and "okv2" not in low
    assert "list=" not in low and "id=" not in low
    assert not any("a" <= c <= "z" for c in low)


def test_fallback_add_and_archive_clean_393():
    r = fallback_reply(collect_successful_writes(_add_msgs()))
    assert "Дача" in r and "грабл" in r.lower() and "лопат" in r.lower() and "перчатк" in r.lower()
    _assert_clean(r)
    ra = fallback_reply(collect_successful_writes(_archive_msgs()))
    assert "Дача" in ra
    _assert_clean(ra)


# ─────────────────────────── reply_has_tech_leak (Opus R4: тире вон) ───────────────────────────

def test_reply_has_tech_leak_no_emdash_393():
    """Opus R4: длинное тире БОЛЬШЕ НЕ утечка (его чистит _postformat) — иначе заземлённый голос с тире
    ложно подменялся. okv2/id=/ref=/hex-id — остаются утечкой. Латиница — контент юзера."""
    assert reply_has_tech_leak("Готово (okv2:added:3).") is True
    assert reply_has_tech_leak("Добавила, id=checklist_x.") is True
    assert reply_has_tech_leak("Дача — готово.") is False
    assert reply_has_tech_leak("Добавила грабли в Дачу.") is False
    assert reply_has_tech_leak("Купила iPhone.") is False


# ─────────────────────────── e2e через handle_turn (легаси-путь) ───────────────────────────

class _StubLLM:
    def __init__(self, scripted: list[AIMessage]) -> None:
        self._scripted, self._i = scripted, 0

    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages):  # noqa: ANN001
        msg = self._scripted[min(self._i, len(self._scripted) - 1)]
        self._i += 1
        return msg


class _CapturingStub(_StubLLM):
    def __init__(self, scripted: list[AIMessage]) -> None:
        super().__init__(scripted)
        self.captured: list = []

    def invoke(self, messages):  # noqa: ANN001
        self.captured = list(messages)
        return super().invoke(messages)


def _add_stub(final: str) -> _StubLLM:
    return _StubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "add_checklist_items",
            "args": {"list_id_or_title": "Дача", "items": ["грабли", "лопату", "перчатки"]},
            "id": "c1"}]),
        AIMessage(content=final),
    ])


async def _turn(db_session, u, llm, text="Добавь в список Дача грабли, лопату и перчатки"):
    return await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:t:{uuid4().hex}", llm=llm, user_text=text,
        inbound_message_id="m1", channel="max")


@pytest.mark.asyncio
async def test_e2e_add_filler_substituted_names_result_393(db_session):
    """Repro-A (P0): успешный add + филлер → финал НАЗЫВАЕТ список и пункты (страховка)."""
    u = seed_telegram_user(db_session); db_session.commit()
    reply = await _turn(db_session, u, _add_stub("Хорошо, приняла к сведению."))
    s = str(reply).lower()
    assert "приняла к сведению" not in s, reply
    assert "дача" in s and "грабл" in s and "лопат" in s and "перчатк" in s, reply


@pytest.mark.asyncio
async def test_e2e_archive_resumed_filler_substituted_393(db_session):
    """Repro-B (P0): archive через confirm → «да» → филлер-дамп → финал НАЗЫВАЕТ «Дача»."""
    u = seed_telegram_user(db_session); db_session.commit()
    ChecklistService(db_session).create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Дача")
    db_session.commit()
    thread = f"react:t:{uuid4().hex}"
    r1 = await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id, thread_id=thread,
        llm=_StubLLM([AIMessage(content="", tool_calls=[{
            "name": "archive_checklist", "args": {"list_id_or_title": "Дача"}, "id": "c1"}])]),
        user_text="Удали список Дача", inbound_message_id="m1", channel="max")
    # #394: confirm-пауза на языке юзера («удаляю»), БЕЗ внутреннего «архив*» (e2e unified-путь).
    assert "удал" in str(r1).lower() and "архив" not in str(r1).lower(), r1
    r2 = await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id, thread_id=thread,
        llm=_StubLLM([AIMessage(content="Вот твои списки дел: Кино, Поход, Дела на сегодня.")]),
        user_text="да", inbound_message_id="m2", channel="max")
    s = str(r2).lower()
    assert "кино" not in s and "поход" not in s, r2
    assert "дача" in s, r2
    # #394: финальный ответ на языке юзера («удалила/убрала»), БЕЗ внутреннего «архив*».
    assert "архив" not in s, r2
    assert "удал" in s or "убр" in s, r2
    # #394: механика не тронута — список реально soft-deleted (status=archived, обратимо).
    from sreda.db.models.checklists import Checklist
    cl = (db_session.query(Checklist)
          .filter(Checklist.tenant_id == u.tenant_id, Checklist.user_id == u.user_id,
                  Checklist.title == "Дача").one())
    assert cl.status == "archived", cl.status


@pytest.mark.asyncio
async def test_e2e_grounded_voice_not_substituted_393(db_session):
    """Негатив-контроль (#121): голос НАЗВАЛ результат → НЕ подменяем (не убить живость)."""
    u = seed_telegram_user(db_session); db_session.commit()
    reply = await _turn(db_session, u, _add_stub(
        "Готово, добавила в «Дача»: грабли, лопату и перчатки. Что-нибудь ещё?"))
    assert "что-нибудь ещё" in str(reply).lower(), reply


@pytest.mark.asyncio
async def test_e2e_grounded_voice_with_emdash_not_substituted_393(db_session):
    """Opus R4: заземлённый голос С длинным тире НЕ подменяется (тире не утечка); _postformat тире→дефис."""
    u = seed_telegram_user(db_session); db_session.commit()
    reply = await _turn(db_session, u, _add_stub(
        "Готово, добавила в «Дача»: грабли, лопату и перчатки — записала. Ещё?"))
    s = str(reply)
    assert "—" not in s, f"тире не вычищено: {reply!r}"
    assert "ещё" in s.lower() and "приняла" not in s.lower(), reply


@pytest.mark.asyncio
async def test_e2e_grounding_note_facts_only_in_prompt_393(db_session):
    """Вариант C: в ПРОМПТ финального прохода инжектится note с ФАКТАМИ, но БЕЗ сырого имени «Дача»."""
    u = seed_telegram_user(db_session); db_session.commit()
    stub = _CapturingStub([
        AIMessage(content="", tool_calls=[{
            "name": "add_checklist_items",
            "args": {"list_id_or_title": "Дача", "items": ["грабли", "лопату", "перчатки"]},
            "id": "c1"}]),
        AIMessage(content="Хорошо, приняла к сведению."),
    ])
    await _turn(db_session, u, stub)
    note = next((str(getattr(m, "content", "")) for m in stub.captured
                 if "успешно выполнено" in str(getattr(m, "content", ""))), "")
    assert note, "grounding_note не инжектнут в промпт финального прохода"
    assert "чек-лист" in note and "3 пункта" in note
    assert "Дача" not in note


@pytest.mark.asyncio
async def test_e2e_okv2_never_reaches_user_393(db_session):
    """R4 (sol): okv2 из ответа модели НИКОГДА не уходит юзеру (_postformat чистит; + подмена)."""
    u = seed_telegram_user(db_session); db_session.commit()
    reply = await _turn(db_session, u, _add_stub("Добавила (okv2:added:3)."))
    assert "okv2" not in str(reply).lower(), reply


@pytest.mark.asyncio
async def test_e2e_undisplayable_item_filler_substituted_to_count_393(db_session):
    """R5 (sol): латиница-пункт + филлер → подмена деградирует к КОЛИЧЕСТВУ (P0-филлер не остаётся)."""
    u = seed_telegram_user(db_session); db_session.commit()
    stub = _StubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "add_shopping_items", "args": {"items": [{"title": "iPhone"}]}, "id": "c1"}]),
        AIMessage(content="Хорошо, приняла к сведению."),
    ])
    reply = await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:t:{uuid4().hex}", llm=stub, user_text="добавь в покупки iPhone",
        inbound_message_id="m1", channel="max")
    s = str(reply).lower()
    assert "приняла к сведению" not in s, reply
    assert "1 пункт" in s and "покупок" in s, reply


@pytest.mark.asyncio
async def test_e2e_destructive_still_confirms_belt_and_suspenders_393(db_session):
    """owner R4-a: разрушающее действие (archive) всё равно упирается в confirm-паузу — #393 его не
    авто-исполняет (belt-and-suspenders: заземление пост-исполнения, а не обход confirm)."""
    u = seed_telegram_user(db_session); db_session.commit()
    ChecklistService(db_session).create_list(tenant_id=u.tenant_id, user_id=u.user_id, title="Дача")
    db_session.commit()
    r1 = await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id, thread_id=f"react:t:{uuid4().hex}",
        llm=_StubLLM([AIMessage(content="", tool_calls=[{
            "name": "archive_checklist", "args": {"list_id_or_title": "Дача"}, "id": "c1"}])]),
        user_text="Удали список Дача", inbound_message_id="m1", channel="max")
    assert getattr(r1, "awaiting_confirm", False), f"разрушающее без паузы: {r1!r}"
    db_session.expire_all()
    lst = ChecklistService(db_session).find_list_by_title(
        tenant_id=u.tenant_id, user_id=u.user_id, needle="Дача")
    assert lst is not None and lst.status == "active", "архивировано ДО подтверждения"
