"""#393 — филлер вместо отчёта о сделанном на пост-tool проходах («сделала, но сказала не то»).

Класс #376. На ходу с УСПЕШНЫМ мутирующим act финальная реплика ОБЯЗАНА называть результат
(имя списка + для add сами пункты), а не отписку/дамп. Вариант владельца D:
  1) заземление голоса — перед финальным проходом инжектим чистую человеческую сводку результата;
  2) страховка — если реплика всё равно не называет результат, подменяем детерминированной
     заземлённой репликой (тёплый шаблон), точка финализации handle_turn (прецедент _declined_reply).
Фикс PATH-AGNOSTIC (легаси + unified), НЕ гейтится _unified_execute_for.

Прод-репро (tenant_max_40921122, 18.07, SGR OFF, guard_scope=legacy):
  A. «Добавь в список Дача грабли, лопату и перчатки» → add_checklist_items ok → «Хорошо, приняла
     к сведению.» (филлер, БД корректна).
  B. «Удали список Дача» → confirm → «да» → archive_checklist ok → «Вот твои списки дел: …»
     (дамп чужих списков вместо «убрала Дачу»); филлер именно на resumed/пост-tool проходе.
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from sreda.runtime import react_loop
from sreda.runtime.react_loop import handle_turn
from sreda.runtime.react_result_report import (
    collect_successful_writes,
    fallback_reply,
    grounding_note,
    reply_grounds_result,
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


_OKV2_ADD = ('okv2:added:{"added_count":3,"duplicate_count":0,'
             '"checklist_id":"checklist_' + "a" * 24 + '",'
             '"created":["грабли","лопату","перчатки"]}')


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
    assert len(acts) == 1, acts
    assert acts[0].target == "Дача"
    assert set(acts[0].items) >= {"грабли", "лопату", "перчатки"}


def test_collect_archive_names_target_393():
    acts = collect_successful_writes(_archive_msgs())
    assert len(acts) == 1 and acts[0].target == "Дача" and acts[0].items == ()


def test_collect_excludes_confirm_decline_393():
    """МУТ-guard: на confirm-ОТКАЗе обёртка вернула «Хорошо, не трогаю.» с result_kind=ok —
    это НЕ успех (нет ok:/okv2: префикса). Иначе отменённый archive → ложный успех (хуже филлера)."""
    msgs = [HumanMessage(content="Удали список Дача"),
            _ai_call("archive_checklist", {"list_id_or_title": "Дача"}),
            _tm("Хорошо, не трогаю.", name="archive_checklist")]
    assert collect_successful_writes(msgs) == ()


def test_collect_excludes_error_result_393():
    """МУТ-guard: error:not_found (список не найден) — не успех."""
    msgs = [HumanMessage(content="Удали список Дача"),
            _ai_call("archive_checklist", {"list_id_or_title": "Дача"}),
            _tm("error:not_found: 'Дача'", name="archive_checklist")]
    assert collect_successful_writes(msgs) == ()


def test_collect_excludes_pure_read_393():
    """Чистое чтение (list_checklists) — не мутирующий act, не заземляем."""
    msgs = [HumanMessage(content="какие у меня списки"),
            _ai_call("list_checklists", {}),
            _tm("Кино; Поход", name="list_checklists")]
    assert collect_successful_writes(msgs) == ()


def test_collect_scopes_to_current_turn_393():
    """Окно — после ПОСЛЕДНЕГО HumanMessage: прошлоходовая запись не считается."""
    msgs = [HumanMessage(content="добавь в дача грабли"),
            _ai_call("add_checklist_items", {"list_id_or_title": "Дача", "items": ["грабли"]}),
            _tm(_OKV2_ADD),
            HumanMessage(content="спасибо")]
    assert collect_successful_writes(msgs) == ()


# ─────────────────────────── reply_grounds_result (детектор) ───────────────────────────

def test_detector_add_filler_not_grounded_393():
    acts = collect_successful_writes(_add_msgs())
    assert reply_grounds_result("Хорошо, приняла к сведению.", acts) is False


def test_detector_add_named_items_grounded_393():
    acts = collect_successful_writes(_add_msgs())
    # add-акт требует И имя списка, И все пункты (sol+terra R2, приёмка «имя списка + пункты»)
    assert reply_grounds_result("Добавила в Дачу грабли, лопату и перчатки.", acts) is True


def test_detector_add_morphology_tolerant_393():
    """Устойчивость к морфологии (класс #180): «грабли/лопата/перчатки» в другой форме — назван."""
    acts = collect_successful_writes(_add_msgs())
    assert reply_grounds_result("Записала грабли, лопата и перчатки в Дачу.", acts) is True


def test_detector_archive_dump_not_grounded_393():
    """Repro-B: дамп ЧУЖИХ списков не называет «Дача» → не заземлён."""
    acts = collect_successful_writes(_archive_msgs())
    assert reply_grounds_result(
        "Вот твои списки дел: Кино, Поход, Дела на сегодня.", acts) is False


def test_detector_archive_named_grounded_393():
    acts = collect_successful_writes(_archive_msgs())
    assert reply_grounds_result("Убрала список «Дача».", acts) is True


# ─────────────────────────── fallback_reply / grounding_note ───────────────────────────

def _assert_clean_user_text(txt: str) -> None:
    low = txt.lower()
    assert "—" not in txt, f"длинное тире запрещено: {txt!r}"
    assert "checklist_" not in low and "okv2" not in low, f"тех-данные в тексте: {txt!r}"
    assert "list=" not in low and "id=" not in low, f"сырые аргументы: {txt!r}"
    # без англицизмов/латиницы в отправляемом тексте
    assert not any("a" <= c <= "z" for c in low), f"латиница в тексте: {txt!r}"


def test_fallback_add_names_result_and_clean_393():
    acts = collect_successful_writes(_add_msgs())
    r = fallback_reply(acts)
    assert "Дача" in r
    low = r.lower()
    assert "грабл" in low and "лопат" in low and "перчатк" in low, r
    _assert_clean_user_text(r)


def test_fallback_archive_names_result_and_clean_393():
    acts = collect_successful_writes(_archive_msgs())
    r = fallback_reply(acts)
    assert "Дача" in r
    _assert_clean_user_text(r)


def test_grounding_note_carries_names_393():
    """Часть 1: сводка для голоса несёт имя списка + пункты (чтобы голос их озвучил)."""
    note = grounding_note(collect_successful_writes(_add_msgs()))
    assert "Дача" in note
    assert "грабл" in note.lower() and "лопат" in note.lower() and "перчатк" in note.lower()


# ─────────────────────────── контракт корректности (Codex terra R1) ───────────────────────────

def test_collect_add_all_dup_no_false_success_393():
    """terra R1 MAJOR-1: add с одними дублями (okv2 created=[]) → нет эффекта → НЕ заземляем
    (иначе «добавила/обновила» = ложный успех, ничего не добавлено)."""
    dup = ('okv2:added_with_dups:{"added_count":0,"duplicate_count":3,"checklist_id":"checklist_'
           + "a" * 24 + '","created":[],"duplicates_existing":["грабли","лопату","перчатки"]}')
    msgs = [HumanMessage(content="добавь в дача грабли"),
            _ai_call("add_checklist_items", {"list_id_or_title": "Дача", "items": ["грабли"]}),
            _tm(dup)]
    assert collect_successful_writes(msgs) == ()


def test_collect_create_checklist_excluded_393():
    """terra R1 MAJOR-1: create_checklist возвращает СУЩЕСТВУЮЩИЙ список при дубле — исход не
    отличить от создания → не заземляем (не врём «завела»)."""
    msgs = [HumanMessage(content="создай список Дача"),
            _ai_call("create_checklist", {"title": "Дача"}),
            _tm("ok:created:checklist_" + "a" * 24 + ":Дача", name="create_checklist")]
    assert collect_successful_writes(msgs) == ()


def test_collect_generic_write_not_grounded_393():
    """Незнакомый write (save_recipe и пр.) вне allowlist → не заземляем (без выдуманного глагола;
    okv2 duplicate = no-op тем более)."""
    msgs = [HumanMessage(content="сохрани рецепт борща"),
            _ai_call("save_recipe", {"title": "Борщ"}),
            _tm('okv2:duplicate:{"recipe_id":"rec_' + "a" * 24 + '","title":"Борщ"}',
                name="save_recipe")]
    assert collect_successful_writes(msgs) == ()


def test_detector_word_boundary_no_false_match_393():
    """terra R1 MAJOR-2: stem «дел» списка «Дела» НЕ должен матчить «Сделала» (подстрока → false-
    grounded → филлер уехал бы юзеру). Токенный матч по границам слов чинит это."""
    msgs = [HumanMessage(content="удали список Дела"),
            _ai_call("archive_checklist", {"list_id_or_title": "Дела"}),
            _tm("ok:archived:checklist_" + "a" * 24, name="archive_checklist")]
    acts = collect_successful_writes(msgs)
    assert len(acts) == 1 and acts[0].target == "Дела"
    assert reply_grounds_result("Сделала, готово.", acts) is False  # не заземлён → подмена
    assert reply_grounds_result("Убрала «Дела».", acts) is True     # назван → голос сохранён


def test_sanitize_emdash_and_id_stripped_393():
    """terra R1 MAJOR-3: длинное тире и id-токен в имени/пункте → вычищены из user-facing текста."""
    created = 'okv2:added:{"added_count":1,"duplicate_count":0,"checklist_id":"checklist_' + \
        "a" * 24 + '","created":["молоко checklist_' + "b" * 24 + '"]}'
    msgs = [HumanMessage(content="добавь"),
            _ai_call("add_checklist_items", {"list_id_or_title": "Дача — лето", "items": ["x"]}),
            _tm(created)]
    r = fallback_reply(collect_successful_writes(msgs))
    assert "—" not in r and "checklist_" not in r.lower(), r
    assert "Дача - лето" in r and "молоко" in r, r  # тире→дефис, id вычищен, имена целы


def test_target_id_only_fail_closed_393():
    """terra R1 MAJOR-3: target-действие с id-именем (не резолвится в человеческое имя) →
    fail-closed, не заземляем (нельзя назвать «что» убрали)."""
    msgs = [HumanMessage(content="удали"),
            _ai_call("archive_checklist", {"list_id_or_title": "checklist_" + "a" * 24}),
            _tm("ok:archived:checklist_" + "a" * 24, name="archive_checklist")]
    assert collect_successful_writes(msgs) == ()


def test_detector_add_partial_items_not_grounded_393():
    """R1 sol+terra MAJOR-2: назван 1 из 3 добавленных пунктов = ЧАСТИЧНЫЙ отчёт → не заземлён
    (полнота: add-акт требует ВСЕ пункты). Страховка добьёт полным списком."""
    acts = collect_successful_writes(_add_msgs())
    assert reply_grounds_result("Добавила грабли.", acts) is False
    assert reply_grounds_result("Добавила грабли и лопату.", acts) is False
    # полный отчёт = имя списка + ВСЕ пункты (R2)
    assert reply_grounds_result("Добавила в Дачу грабли, лопату и перчатки.", acts) is True


def test_detector_all_acts_required_393():
    """R1 sol MAJOR-2: два успешных акта — реплика должна назвать РЕЗУЛЬТАТ КАЖДОГО (не только один)."""
    msgs = [HumanMessage(content="добавь в дача грабли и удали список Поход"),
            _ai_call("add_checklist_items",
                     {"list_id_or_title": "Дача", "items": ["грабли"]}, cid="c1"),
            _ai_call("archive_checklist", {"list_id_or_title": "Поход"}, cid="c2"),
            _tm('okv2:added:{"added_count":1,"duplicate_count":0,"checklist_id":"checklist_'
                + "a" * 24 + '","created":["грабли"]}', cid="c1"),
            _tm("ok:archived:checklist_" + "b" * 24, name="archive_checklist", cid="c2")]
    acts = collect_successful_writes(msgs)
    assert len(acts) == 2
    assert reply_grounds_result("Добавила грабли.", acts) is False          # Поход не назван
    assert reply_grounds_result("Добавила грабли в Дачу, убрала Поход.", acts) is True


# ─────────────────────────── R2 (Codex sol+terra): полнота детектора + fail-closed чистоты ───────────────────────────

def test_detector_add_requires_target_393():
    """R2: add-акт заземлён ТОЛЬКО если названо имя списка. Названы все пункты, но НЕ «Дача» → подмена."""
    acts = collect_successful_writes(_add_msgs())
    assert reply_grounds_result("Добавила грабли, лопату и перчатки.", acts) is False


def test_detector_multiword_all_tokens_393():
    """R2: многословный пункт заземлён, только когда названы ВСЕ его значимые слова (не одно общее):
    «молоко без лактозы» по одному «молоко» → нет."""
    created = ('okv2:added:{"added_count":1,"duplicate_count":0,"checklist_id":"checklist_'
               + "a" * 24 + '","created":["молоко без лактозы"]}')
    msgs = [HumanMessage(content="добавь в покупки молоко без лактозы"),
            _ai_call("add_shopping_items", {"items": ["молоко без лактозы"]}),
            _tm(created, name="add_shopping_items")]
    acts = collect_successful_writes(msgs)
    assert len(acts) == 1
    assert reply_grounds_result("Молоко записала.", acts) is False
    assert reply_grounds_result("Записала молоко без лактозы.", acts) is True


def test_collect_add_latin_item_fail_closed_393():
    """R2: латиница в пункте (не «чистый русский») → неотображаемо → fail-closed ВЕСЬ акт."""
    created = ('okv2:added:{"added_count":1,"duplicate_count":0,"checklist_id":"checklist_'
               + "a" * 24 + '","created":["milk"]}')
    msgs = [HumanMessage(content="добавь"),
            _ai_call("add_checklist_items", {"list_id_or_title": "Дача", "items": ["milk"]}),
            _tm(created)]
    assert collect_successful_writes(msgs) == ()


def test_collect_add_latin_target_fail_closed_393():
    """R2: латиница в имени списка → fail-closed (target checklist-add обязан быть чистым русским).
    Чисто-латинское «Weekly Plan» И СМЕШАННОЕ «Дача Todo» (кириллица+латиница) — оба отклоняются
    (смешанное изолирует именно latin-гард: гард «есть кириллица» его пропустил бы)."""
    for bad in ("Weekly Plan", "Дача Todo"):
        msgs = [HumanMessage(content="добавь"),
                _ai_call("add_checklist_items", {"list_id_or_title": bad, "items": ["молоко"]}),
                _tm(_OKV2_ADD)]
        assert collect_successful_writes(msgs) == (), bad


def test_collect_add_id_target_fail_closed_393():
    """R2: checklist-add с id-именем списка → fail-closed (нельзя назвать «в какой список»)."""
    msgs = [HumanMessage(content="добавь"),
            _ai_call("add_checklist_items",
                     {"list_id_or_title": "checklist_" + "a" * 24, "items": ["молоко"]}),
            _tm(_OKV2_ADD)]
    assert collect_successful_writes(msgs) == ()


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
    """Записывает последний вход модели — для проверки инъекции сводки (часть 1)."""

    def __init__(self, scripted: list[AIMessage]) -> None:
        super().__init__(scripted)
        self.captured: list = []

    def invoke(self, messages):  # noqa: ANN001
        self.captured = list(messages)
        return super().invoke(messages)


@pytest.mark.asyncio
async def test_e2e_add_filler_substituted_names_result_393(db_session):
    """Repro-A (P0): успешный add + филлер → финал НАЗЫВАЕТ список и пункты (страховка)."""
    u = seed_telegram_user(db_session); db_session.commit()
    stub = _StubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "add_checklist_items",
            "args": {"list_id_or_title": "Дача", "items": ["грабли", "лопату", "перчатки"]},
            "id": "c1"}]),
        AIMessage(content="Хорошо, приняла к сведению."),  # филлер
    ])
    reply = await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:t:{uuid4().hex}", llm=stub,
        user_text="Добавь в список Дача грабли, лопату и перчатки",
        inbound_message_id="m1", channel="max")
    s = str(reply).lower()
    assert "приняла к сведению" not in s, f"филлер не подменён: {reply!r}"
    assert "дача" in s, reply
    assert "грабл" in s and "лопат" in s and "перчатк" in s, reply


@pytest.mark.asyncio
async def test_e2e_archive_resumed_filler_substituted_names_result_393(db_session):
    """Repro-B (P0): archive через confirm → «да» → филлер-дамп → финал НАЗЫВАЕТ «Дача»."""
    u = seed_telegram_user(db_session); db_session.commit()
    # список должен существовать, иначе archive → not_found
    ChecklistService(db_session).create_list(
        tenant_id=u.tenant_id, user_id=u.user_id, title="Дача")
    db_session.commit()
    thread = f"react:t:{uuid4().hex}"
    r1 = await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id, thread_id=thread,
        llm=_StubLLM([AIMessage(content="", tool_calls=[{
            "name": "archive_checklist", "args": {"list_id_or_title": "Дача"}, "id": "c1"}])]),
        user_text="Удали список Дача", inbound_message_id="m1", channel="max")
    assert "архивир" in str(r1).lower() or "убер" in str(r1).lower(), r1  # confirm-пауза
    r2 = await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id, thread_id=thread,
        llm=_StubLLM([AIMessage(content="Вот твои списки дел: Кино, Поход, Дела на сегодня.")]),
        user_text="да", inbound_message_id="m2", channel="max")
    s = str(r2).lower()
    assert "кино" not in s and "поход" not in s, f"дамп чужих списков не подменён: {r2!r}"
    assert "дача" in s, r2


@pytest.mark.asyncio
async def test_e2e_grounded_voice_not_substituted_393(db_session):
    """Негативный контроль (#121): голос НАЗВАЛ результат → НЕ подменяем (не убить живость)."""
    u = seed_telegram_user(db_session); db_session.commit()
    voiced = "Готово, добавила в «Дача»: грабли, лопату и перчатки. Что-нибудь ещё?"
    stub = _StubLLM([
        AIMessage(content="", tool_calls=[{
            "name": "add_checklist_items",
            "args": {"list_id_or_title": "Дача", "items": ["грабли", "лопату", "перчатки"]},
            "id": "c1"}]),
        AIMessage(content=voiced),
    ])
    reply = await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:t:{uuid4().hex}", llm=stub,
        user_text="Добавь в список Дача грабли, лопату и перчатки",
        inbound_message_id="m1", channel="max")
    assert "что-нибудь ещё" in str(reply).lower(), f"живой голос подменён: {reply!r}"


@pytest.mark.asyncio
async def test_e2e_grounding_note_injected_into_model_input_393(db_session):
    """Часть 1: перед финальным проходом сводка результата (имя+пункты) попадает во ВХОД модели."""
    u = seed_telegram_user(db_session); db_session.commit()
    stub = _CapturingStub([
        AIMessage(content="", tool_calls=[{
            "name": "add_checklist_items",
            "args": {"list_id_or_title": "Дача", "items": ["грабли", "лопату", "перчатки"]},
            "id": "c1"}]),
        AIMessage(content="Хорошо, приняла к сведению."),
    ])
    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=f"react:t:{uuid4().hex}", llm=stub,
        user_text="Добавь в список Дача грабли, лопату и перчатки",
        inbound_message_id="m1", channel="max")
    blob = "\n".join(str(getattr(m, "content", "")) for m in stub.captured)
    # отличительный маркер именно НАШЕЙ сводки (имена «Дача»/«грабли» и так в истории —
    # проверяем, что инжектнут grounding_note, а не просто присутствие имён).
    assert "успешно выполнила" in blob, "grounding_note не инжектнут во вход финального прохода"
    assert "Дача" in blob and "грабл" in blob.lower(), blob
