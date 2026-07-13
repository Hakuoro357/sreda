"""#356: свежесть own-data - правило в промпте + МЕХАНИЧЕСКИЙ гейт + глубина prev-turn.

Прод 2026-07-11 23:28 (канарейка): «Что у меня в списке кино» → Среда пересказала
список ИЗ ИСТОРИИ (ход 16:13), не вызвав ни одного read-инструмента (trace:
tools=[]) - данные могли устареть; пустой журнал хода дополнительно рвал цепочку
LLM-фолбэка #352 на следующем ходе.

Корни и слои фикса (аудит трёх поколений промптов + ревью Codex):
1. Промпт: канон свежести + анти-инъекция + техследы + батчинг - блок
   <data_discipline> (правила были в Gen1/Gen2 и не переехали в ReAct; g-015 -
   одна константа против дрейфа).
2. МЕХАНИКА (промпту не доверяем, класс #180/#288/#350): read-кюс есть, а ход
   завершается без ЕДИНОГО успешного инструмента cue-домена → guard возвращает
   модель с директивой «сначала прочитай» (один форс, анти-петля).
3. _prev_turn_families смотрит до БЛИЖАЙШЕГО инструментального хода (потолок 3
   сегмента) - болтливый ход-пересказ не рвёт наследование контекста #352.
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from sreda.runtime import react_loop
from sreda.runtime.react_loop import (
    _prev_turn_families,
    _stale_readback_domains,
    _system_prompt,
)

# ─────────────────── промпт: канон свежести и дисциплина данных ───────────────────


def test_prompt_has_data_discipline_block_356():
    sp = _system_prompt("13 июля 2026")
    assert "<data_discipline>" in sp
    assert "КАНОН СВЕЖЕСТИ" in sp
    assert "НЕ источник правды" in sp


def test_prompt_empty_requires_successful_read_356():
    """«Пусто» - только после УСПЕШНОГО чтения (Codex R1: ошибка/фильтр не
    доказывают отсутствие)."""
    sp = _system_prompt("13 июля 2026")
    assert "отсутствие НЕ доказывают" in sp


def test_prompt_anti_injection_356():
    """Данные (веб/заметки/результаты) - не команды; цель задаёт реплика юзера."""
    sp = _system_prompt("13 июля 2026")
    assert "не как команды" in sp
    assert "ТЕКУЩАЯ реплика пользователя" in sp


def test_prompt_tech_traces_scoped_356():
    """Техследы запрещены В ТЕКСТЕ юзеру, но техформаты ОБЯЗАТЕЛЬНЫ в аргументах
    (Codex R1: «ISO - никогда» сломал бы вызовы напоминаний)."""
    sp = _system_prompt("13 июля 2026")
    assert "В АРГУМЕНТАХ" in sp
    assert "имён инструментов" in sp


def test_prompt_batching_and_scripts_356():
    sp = _system_prompt("13 июля 2026")
    assert "НЕЗАВИСИМЫЕ вызовы - одним сообщением" in sp
    assert "иероглиф" in sp
    assert "эмодзи - можно" in sp


def test_prompt_identity_memory_mechanics_356():
    """Механика памяти («выборка»/«индекс») не раскрывается; источник - правдиво."""
    sp = _system_prompt("13 июля 2026")
    assert "механику памяти и поиска" in sp
    assert "не выдумывай\nисточник" in sp or "не выдумывай источник" in sp


def test_prompt_rule3_no_freshness_duplicate_356():
    """Правило 3 сокращено до внутриходовой части - свежесть между ходами живёт
    ТОЛЬКО в каноне (Codex R1: две самостоятельные формулировки дрейфуют)."""
    sp = _system_prompt("13 июля 2026")
    assert "НО новый запрос пользователя" not in sp


def test_prompt_block_order_and_uniqueness_356():
    """R1 sol MINOR: канон один, стоит ПОСЛЕ rules и ДО динамического хвоста
    (кеш-дисциплина: стабильный префикс не рвётся динамикой)."""
    sp = _system_prompt("13 июля 2026", persona_overlay="ЛАСКОВЕЕ")
    assert sp.count("<data_discipline>") == 1
    assert sp.index("</rules>") < sp.index("<data_discipline>")
    assert sp.index("</data_discipline>") < sp.index("<style_preset>")
    assert sp.index("</data_discipline>") < sp.index("<context>")


def test_prompt_write_commands_exempt_356():
    """R1 sol M1: канон не должен заставлять читать перед каждой записью."""
    sp = _system_prompt("13 июля 2026")
    assert "Чистая\nкоманда ИЗМЕНЕНИЯ" in sp or "Чистая команда ИЗМЕНЕНИЯ" in sp\
        or "команда ИЗМЕНЕНИЯ" in sp


# ─────────────────── _stale_readback_domains: детектор гейта ───────────────────


def _turn(user, *msgs):
    return [HumanMessage(content=user), *msgs, AIMessage(content="Вот список: кино.")]


def _tm(name, kind=None, cid="t1"):
    art = {"result_kind": kind} if kind else None
    return ToolMessage(content="ok", name=name, tool_call_id=cid, artifact=art)


def _ai_tc(name, cid="t1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": cid}])


def test_stale_cue_without_read_fires_356():
    """Инцидент: read-кюс есть, инструментов нет → форс."""
    msgs = _turn("Что у меня в списке кино")
    assert "checklists" in _stale_readback_domains(msgs)


def test_stale_read_same_domain_clears_356():
    msgs = _turn("Что у меня в списке кино",
                 _ai_tc("list_checklist_items"), _tm("list_checklist_items"))
    assert _stale_readback_domains(msgs) == frozenset()


def test_stale_write_intent_never_fires_356():
    """R1 sol M1/M2: write-intent ход (write_command_signal) - юрисдикция записи,
    не гейта: «добавь молоко в покупки» несёт shopping-кюс (щедрый, p-014), но
    форс чтения на write-ходе = лишний вызов, а на УПАВШЕЙ записи - ложный
    recovery в чтение вместо повтора записи. Гейт молчит даже без инструментов."""
    assert _stale_readback_domains(_turn("добавь молоко в покупки")) == frozenset()
    msgs = _turn("добавь молоко в покупки",
                 _ai_tc("add_shopping_items"), _tm("add_shopping_items", "ok"))
    assert _stale_readback_domains(msgs) == frozenset()


def test_stale_error_read_still_fires_356():
    """R1 sol/terra: ошибка чтения (result_kind=error) свежесть НЕ доказывает."""
    msgs = _turn("Что у меня в списке кино",
                 _ai_tc("list_checklist_items"), _tm("list_checklist_items", "error"))
    assert "checklists" in _stale_readback_domains(msgs)


def test_stale_write_tool_on_read_phrase_not_enough_356():
    """R1 sol M5: на read-фразе write-вызов домена свежесть показа не доказывает
    (только чтение гасит)."""
    msgs = _turn("Что у меня в списке кино",
                 _ai_tc("mark_checklist_item_done"), _tm("mark_checklist_item_done", "ok"))
    assert "checklists" in _stale_readback_domains(msgs)


def test_stale_independent_domains_and_semantics_356():
    """R1 sol/terra M4: независимые разделы («покажи задачи и покупки») кроются
    ПО-ДОМЕННО - чтение задач не закрывает покупки."""
    msgs = _turn("покажи задачи и покупки",
                 _ai_tc("list_tasks"), _tm("list_tasks"))
    assert _stale_readback_domains(msgs) == frozenset({"shopping"})


def test_stale_or_group_single_read_covers_356():
    """Группа-требование: «что в списке кино» - альтернативы ОДНОГО слова
    («список» → checklists|shopping) - чтение чек-листов покрывает запрос."""
    msgs = _turn("что в списке кино",
                 _ai_tc("list_checklist_items"), _tm("list_checklist_items"))
    assert _stale_readback_domains(msgs) == frozenset()


def test_stale_explicit_shopping_not_covered_by_checklist_read_356():
    """R2 terra/sol/субагент: «покажи список покупок» - «покупки» названы ЯВНО
    (своя группа-требование) - чтение чек-листов покупки НЕ закрывает."""
    msgs = _turn("покажи список покупок",
                 _ai_tc("list_checklist_items"), _tm("list_checklist_items"))
    assert "shopping" in _stale_readback_domains(msgs)


def test_stale_two_explicit_groups_and_356():
    """R2 субагент MINOR-3: «покажи чек-листы и покупки» - два явных требования,
    чтение одного не схлопывает второе (группы по паттернам, не по объединению)."""
    msgs = _turn("покажи чек-листы и покупки",
                 _ai_tc("list_checklist_items"), _tm("list_checklist_items"))
    assert "shopping" in _stale_readback_domains(msgs)


def test_stale_status_error_not_fresh_356():
    """R2 sol M4: ToolMessage(status="error") БЕЗ artifact - не свежесть."""
    err = ToolMessage(content="boom", name="list_checklist_items",
                      tool_call_id="t1", status="error")
    msgs = _turn("Что у меня в списке кино", _ai_tc("list_checklist_items"), err)
    assert "checklists" in _stale_readback_domains(msgs)


def test_stale_error_content_with_ok_kind_not_fresh_356():
    """R3 terra: семантическая ошибка контрактной строкой «error: …» при
    result_kind=ok - не свежесть."""
    err = ToolMessage(content="error: internal", name="list_checklist_items",
                      tool_call_id="t1", artifact={"result_kind": "ok"})
    msgs = _turn("Что у меня в списке кино", _ai_tc("list_checklist_items"), err)
    assert "checklists" in _stale_readback_domains(msgs)


def test_stale_qualified_list_single_read_covers_356():
    """R3 sol M2: «покажи список задач» - «список» лишь обёртка, домен задаёт
    квалификатор: чтение задач закрывает запрос ЦЕЛИКОМ (нет ложного форса
    checklists/shopping)."""
    msgs = _turn("покажи список задач", _ai_tc("list_tasks"), _tm("list_tasks"))
    assert _stale_readback_domains(msgs) == frozenset()
    msgs2 = _turn("покажи список напоминаний",
                  _ai_tc("list_reminders"), _tm("list_reminders"))
    assert _stale_readback_domains(msgs2) == frozenset()


def test_stale_no_cue_no_fire_356():
    assert _stale_readback_domains(_turn("спасибо")) == frozenset()
    assert _stale_readback_domains(_turn("я посмотрел машину войны")) == frozenset()


def test_stale_not_executed_read_still_fires_356():
    """domain_blocked/family_not_loaded - НЕ исполнение: форс остаётся."""
    msgs = _turn("Что у меня в списке кино",
                 _ai_tc("list_checklist_items"),
                 _tm("list_checklist_items", "family_not_loaded"))
    assert "checklists" in _stale_readback_domains(msgs)


def test_stale_foreign_domain_read_still_fires_356():
    """Чтение ЧУЖОГО домена кюс не гасит (спросил списки - прочитал напоминания)."""
    msgs = _turn("Что у меня в списке кино",
                 _ai_tc("list_reminders"), _tm("list_reminders"))
    assert "checklists" in _stale_readback_domains(msgs)


def test_stale_empty_on_no_messages_356():
    assert _stale_readback_domains(None) == frozenset()
    assert _stale_readback_domains([]) == frozenset()


# ─────────────────── _prev_turn_families: глубина до инструментального хода ───────────────────


def _closed(user, *tools):
    msgs: list = [HumanMessage(content=user)]
    for i, n in enumerate(tools):
        msgs.append(AIMessage(content="", tool_calls=[{"name": n, "args": {}, "id": f"t{i}"}]))
        msgs.append(ToolMessage(content="ok", name=n, tool_call_id=f"t{i}"))
    msgs.append(AIMessage(content="Вот."))
    return msgs


def test_prev_families_skips_chatty_turn_356():
    """Вчерашний прод-кейс: инструментальный ход → болтливый пересказ →
    themeless-продолжение видит разделы ЧЕРЕЗ пересказ (глубина 2)."""
    msgs = _closed("Что у меня в списке кино", "list_checklist_items") \
        + _closed("расскажи анекдот")
    assert _prev_turn_families(msgs) == ("checklists",)


def test_prev_families_two_chatty_turns_356():
    msgs = _closed("Что у меня в списке кино", "list_checklist_items") \
        + _closed("расскажи анекдот") + _closed("смешно")
    assert _prev_turn_families(msgs) == ("checklists",)


def test_prev_families_depth_capped_at_three_356():
    """Потолок 3 сегмента: инструментальный ход глубже - НЕ контекст."""
    msgs = _closed("Что у меня в списке кино", "list_checklist_items") \
        + _closed("расскажи анекдот") + _closed("ещё один") + _closed("смешно")
    assert _prev_turn_families(msgs) == ()


def test_prev_families_nearest_instrumental_wins_356():
    """Берётся БЛИЖАЙШИЙ инструментальный ход, не объединение по глубине."""
    msgs = _closed("Что у меня в списке кино", "list_checklist_items") \
        + _closed("покажи напоминания", "list_reminders")
    assert _prev_turn_families(msgs) == ("reminders",)


# ─────────────────── e2e через handle_turn (паттерн #352) ───────────────────


class _NoTrace:
    def __getattr__(self, name):
        return lambda *a, **k: None


class _Chat:
    def __init__(self, name, responses=None, domain_reply="checklists"):
        self.name = name
        self._responses = list(responses or [])
        self.domain_calls: list = []
        # строка = один ответ на все вызовы; список = очередь по вызовам
        self._domain_replies = (list(domain_reply)
                                if isinstance(domain_reply, list) else None)
        self._domain_reply = domain_reply

    def bind_tools(self, tools):
        return self

    def invoke(self, messages, **kwargs):
        self.seen_batches = getattr(self, "seen_batches", [])
        self.seen_batches.append(messages)
        if self._responses:
            return self._responses.pop(0)
        if getattr(self, "echo_tool_result", False):
            # R3 sol: финал строится ИЗ последнего ToolMessage - маркер свежести
            # физически приходит из инструмента, не из скрипта теста
            _tools = [m for m in messages if isinstance(m, ToolMessage)]
            if _tools:
                return AIMessage(content=f"Вот свежий список: {_tools[-1].content}")
        return AIMessage(content="ок")

    async def ainvoke(self, messages, **kwargs):
        sys = str(getattr(messages[0], "content", ""))
        if "классификатор РАЗДЕЛА" in sys:
            self.domain_calls.append(str(getattr(messages[-1], "content", "")))
            if self._domain_replies is not None:
                return AIMessage(content=self._domain_replies.pop(0))
            return AIMessage(content=self._domain_reply)
        raise RuntimeError("ainvoke not mocked for this classifier")


def _clean_tool(name, inv):
    def _f(**kwargs):
        inv[name] = inv.get(name, 0) + 1
        if name.startswith("list") and "__fresh_payload__" in inv:
            return str(inv["__fresh_payload__"])  # R3 sol: fresh-маркер ИЗ инструмента
        return f"{name}-ok"
    return StructuredTool.from_function(func=_f, name=name, description=name)


_CORE = ["list_checklist_items", "mark_checklist_item_done", "add_shopping_items",
         "list_reminders", "schedule_reminder", "add_task",
         "need_family", "recall_memory", "ask_human"]
_WEB = ["web_search", "fetch_url", "get_weather"]


@pytest.fixture
def install(monkeypatch):
    from sreda.config import settings as settings_mod

    def _install(*, responses, domain_reply="checklists"):
        monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "1")
        monkeypatch.setenv("SREDA_REACT_UNIFIED_PATH_ENABLED", "1")
        monkeypatch.setenv("SREDA_REACT_UNIFIED_TENANTS", "t")
        monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_MODE", "execute")
        monkeypatch.setenv("SREDA_REACT_DOMAIN_SCOPE_EXECUTE_TENANTS", "*")
        monkeypatch.setenv("SREDA_REACT_PRUNE_TENANTS", "*")
        settings_mod.get_settings.cache_clear()
        inv: dict = {}
        monkeypatch.setattr(react_loop, "build_slice_tools", lambda *a, **k: [
            _clean_tool(n, inv) for n in (_CORE + _WEB)])
        monkeypatch.setattr(react_loop, "_trace", _NoTrace())
        monkeypatch.setattr(react_loop, "_record_react_usage", lambda **k: None)

        def _interrupt(payload):
            inv["interrupt"] = inv.get("interrupt", 0) + 1
            return "да"
        monkeypatch.setattr(react_loop, "interrupt", _interrupt)
        import sreda.services.llm as llm_mod
        freddie = _Chat("freddie", responses=responses, domain_reply=domain_reply)
        monkeypatch.setattr(llm_mod, "get_chat_llm", lambda *a, **k: _Chat("ds"))
        return inv, freddie

    yield _install
    settings_mod.get_settings.cache_clear()


def _run(freddie, thread, text):
    return asyncio.run(react_loop.handle_turn(
        session=None, tenant_id="t", user_id="u", thread_id=thread,
        llm=freddie, user_text=text, inbound_message_id=f"{thread}:{text[:10]}",
        channel="react", resume_only=False, expected_confirm_id="",
        provider_key="inception-mercury2", fallback_llm=None))


def _ai_call(tool, cid, **args):
    return AIMessage(content="", tool_calls=[{"name": tool, "args": args, "id": cid}])


def test_e2e_freshness_gate_forces_read_356(install):
    """Прод-кейс 23:28: модель отвечает пересказом БЕЗ чтения → гейт возвращает
    её читать → ответ построен из свежего чтения."""
    inv, freddie = install(responses=[
        AIMessage(content="Вот твой список кино: Скорпион, Машина войны."),  # пересказ
        _ai_call("list_checklist_items", "c1", name="кино"),                 # после форса
    ])
    freddie.echo_tool_result = True  # финал = эхо ToolMessage (R3 sol: маркер ИЗ инструмента)
    inv["__fresh_payload__"] = "Мастодонт-9000"
    r = _run(freddie, "f1", "Что у меня в списке кино")
    assert inv.get("list_checklist_items", 0) == 1, \
        "гейт свежести заставил прочитать (промпт модель проигнорила)"
    # R2/R3 sol/terra: маркер в финале пришёл ФИЗИЧЕСКИ из результата инструмента
    assert "мастодонт-9000" in str(r).lower()
    assert "скорпион" not in str(r).lower()


def test_e2e_preflight_off_rollback_untouched_356(install, monkeypatch):
    """R2 все трое (byte-identical rollback): preflight OFF (eff=None) - гейт
    свежести молчит И в route, И в guard: refusal-путь получает ЛЕГАСИ-recovery
    нудж, не freshness-нудж (иначе откат преflight не байт-идентичен)."""
    inv, freddie = install(responses=[
        AIMessage(content="Я не умею показывать списки."),   # refusal → guard (легаси)
        AIMessage(content="Вот, нашла."),
    ])
    monkeypatch.setenv("SREDA_REACT_PREFLIGHT_ENABLED", "0")
    monkeypatch.setenv("SREDA_REACT_UNIFIED_PATH_ENABLED", "0")
    from sreda.config import settings as settings_mod
    settings_mod.get_settings.cache_clear()
    _run(freddie, "f-off", "Что у меня в списке кино")
    assert len(freddie.seen_batches) >= 2, "refusal-recovery дал второй проход"
    _all2 = "\n".join(str(getattr(m, "content", "")) for m in freddie.seen_batches[1])
    assert "запросил СВОИ данные" not in _all2, \
        "freshness-нудж НЕ должен подменять легаси-recovery на rollback-конфиге"


def test_e2e_freshness_gate_kill_switch_356(install, monkeypatch):
    """R1 субагент MAJOR (g-065): OFF-флаг выключает гейт без деплоя -
    поведение как до фикса (пересказ выпускается первым проходом)."""
    inv, freddie = install(responses=[
        AIMessage(content="Вот список: кино (пересказ)."),
    ])
    monkeypatch.setenv("SREDA_REACT_FRESHNESS_GATE", "0")
    from sreda.config import settings as settings_mod
    settings_mod.get_settings.cache_clear()
    r = _run(freddie, "f0", "Что у меня в списке кино")
    assert inv.get("list_checklist_items", 0) == 0, "OFF: форса нет"
    assert "пересказ" in str(r).lower()


def test_e2e_freshness_gate_single_force_no_loop_356(install):
    """Анти-петля: модель игнорит форс дважды → ход всё равно завершается
    (один форс, потом выпуск с логом - осознанный компромисс щедрого кюса)."""
    inv, freddie = install(responses=[
        AIMessage(content="Вот список: кино."),
        AIMessage(content="Вот список: кино (упрямо из головы)."),
    ])
    r = _run(freddie, "f2", "Что у меня в списке кино")
    assert "кино" in str(r).lower(), "ход завершился, не завис"
    assert inv.get("list_checklist_items", 0) == 0


def test_e2e_freshness_gate_not_on_write_turn_356(install):
    """Анти-регресс: write-ход с кюсом («добавь молоко в покупки») - записала
    и ответила, гейт лишний проход НЕ добавляет."""
    inv, freddie = install(responses=[
        _ai_call("add_shopping_items", "c1", items=["молоко"]),
        AIMessage(content="Добавила молоко."),
        AIMessage(content="ЛИШНИЙ ПРОХОД - гейт не должен сюда дойти"),
    ])
    r = _run(freddie, "f3", "добавь молоко в покупки")
    assert "добавила" in str(r).lower()
    assert "лишний" not in str(r).lower()


def test_e2e_freshness_gate_skips_smalltalk_356(install):
    inv, freddie = install(responses=[AIMessage(content="Пожалуйста!")])
    r = _run(freddie, "f4", "спасибо")
    assert "пожалуйста" in str(r).lower()


def test_e2e_full_incident_chain_readback_then_mark_356(install):
    """Полная вчерашняя цепочка с обоими фиксами: список (чтение) → болтливый
    ход без инструментов (классификатор честно вызывается - живой Фредди ответил
    бы none; глубина видит контекст через него) → «я посмотрел машину войны» →
    отметка через confirm."""
    inv, freddie = install(responses=[
        _ai_call("list_checklist_items", "c1", name="кино"),
        AIMessage(content="Вот твой список кино: Скорпион, Машина войны."),
        AIMessage(content="Ха, смешной анекдот вышел."),                 # болтливый ход
        _ai_call("mark_checklist_item_done", "c3", name="кино", item="Машина войны"),
        AIMessage(content="Отметила «Машина войны»."),
    ], domain_reply=["none", "checklists"])
    _run(freddie, "f5", "Что у меня в списке кино")
    assert freddie.domain_calls == [], "кюсный ход - классификатор не нужен"
    _run(freddie, "f5", "расскажи анекдот")
    assert len(freddie.domain_calls) == 1, "болтовня с prev-контекстом - вызов есть"
    assert inv.get("mark_checklist_item_done", 0) == 0, "none → ничего не открыто"
    _run(freddie, "f5", "я посмотрел машину войны")
    assert len(freddie.domain_calls) == 2
    assert "Предыдущий ход работал с разделом: checklists" in freddie.domain_calls[1], \
        "глубина: контекст виден ЧЕРЕЗ болтливый ход"
    assert inv.get("interrupt", 0) == 1, "отметка через кандидат-confirm"
    assert inv.get("mark_checklist_item_done", 0) == 1
