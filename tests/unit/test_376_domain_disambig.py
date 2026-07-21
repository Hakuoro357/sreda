"""#376: subtract-only дизамбигуация неоднозначной read-кюс-группы.

На «Что у меня в списке кино» щедрый read_cue кладёт в allowed_read {checklists, shopping}
(слово «список» → группа), и mercury может позвать list_shopping (промах +покупки, прод
2026-07-15). Умный классификатор (disambiguator) стабильно говорит «checklists» → его
high-вердикт ВЫЧИТАЕТ остальных членов неоднозначной группы из allowed_read.

Инварианты (owner + ревью-трио R1/R2):
- ТОЛЬКО вычитание (add невозможен by construction): Фредди-домен вне поднятых → не применяем.
- write не трогаем (allowed_write исключён из subtract).
- compound/cross пропускаем (reminders/menu целы).
- disambiguator=None → байт-в-байт текущее поведение (#285/#352).

RED до реализации: параметр disambiguator в compute_unified_policy ещё не существует.
"""
from __future__ import annotations

from sreda.runtime.react_policy import compute_unified_policy
from sreda.runtime.react_preflight import DomainClassResult, route_domains


def _pol(text, disambiguator=None):
    return compute_unified_policy(text, route_domains(text), disambiguator=disambiguator)


def test_disambig_subtracts_shopping_376():
    """«список кино» + Фредди=checklists/high → shopping ВЫЧТЕН из allowed_read."""
    pol = _pol("Что у меня в списке кино", DomainClassResult(("checklists",), "high"))
    assert "checklists" in pol["allowed_read"]
    assert "shopping" not in pol["allowed_read"]  # член неоднозначной группы вычтен


def test_disambig_off_byte_identical_376():
    """disambiguator=None → shopping остаётся (текущее поведение не тронуто)."""
    pol = _pol("Что у меня в списке кино", None)
    assert "shopping" in pol["allowed_read"]


def test_disambig_low_untouched_376():
    """Фредди low → политику НЕ трогаем (fail-safe)."""
    pol = _pol("Что у меня в списке кино", DomainClassResult((), "low"))
    assert "shopping" in pol["allowed_read"]


def test_disambig_add_not_applied_376():
    """Фредди-домен ВНЕ поднятых (add-режим, возможная инъекция) → НЕ применяем.
    «покажи задачи» поднимает tasks; Фредди говорит shopping (не поднято) → allowed_read не трогаем."""
    text = "покажи задачи"
    pol = _pol(text, DomainClassResult(("shopping",), "high"))
    # shopping НЕ добавлен (add запрещён — только вычитание из поднятых)
    assert "shopping" not in pol["allowed_read"]


def test_disambig_compound_untouched_376():
    """Составной «кино и напомни хлеб» (compound) → дизамбигуация ПРОПУЩЕНА целиком:
    allowed_read с дизамбигуатором == без него (мой код не вмешивается в compound)."""
    text = "покажи список кино и напомни купить хлеб"
    r = route_domains(text)
    assert r.compound_by_connector, "тест-предпосылка: это compound"
    base = compute_unified_policy(text, r)
    dis = compute_unified_policy(text, r, disambiguator=DomainClassResult(("checklists",), "high"))
    assert base["allowed_read"] == dis["allowed_read"], "compound: дизамбигуация не должна менять read"
    assert dis["signals"]["disambig_kind"] is None, "compound: дизамбигуация должна быть пропущена"


def test_disambig_cross_untouched_376():
    """Cross «покупки из меню» → дизамбигуация пропущена, menu+shopping целы."""
    text = "составь покупки из меню"
    r = route_domains(text)
    pol = compute_unified_policy(text, r, disambiguator=DomainClassResult(("shopping",), "high"))
    if r.cross_intent:  # направленное кросс-намерение
        assert "menu" in pol["allowed_read"] and "shopping" in pol["allowed_read"]


def test_disambig_does_not_touch_write_376():
    """subtract НЕ снимает домен, который является write-целью (allowed_read ⊇ allowed_write инвариант)."""
    pol = _pol("Что у меня в списке кино", DomainClassResult(("checklists",), "high"))
    for wd in pol["allowed_write"]:
        assert wd in pol["allowed_read"], "write-цель осталась читаемой"


def test_disambig_none_full_equality_376():
    """CR R1 sol/terra MAJOR (+R2 MINOR обоих): disambiguator=None → policy-dict ПОЛНОСТЬЮ
    в до-#376 форме — golden-заморозка НАБОРА ключей policy и signals (не только двух полей).
    Регресс «поле просочилось при OFF» красит этот тест."""
    from sreda.runtime.react_preflight import route_domains as _rd
    GOLDEN_POLICY_KEYS = {"v", "mode", "allowed_read", "allowed_write", "confirm_write", "signals"}
    GOLDEN_SIGNAL_KEYS = {"write_cmd", "declarative", "read_cues", "sticky_memory",
                          "turn_continuation", "llm_domains"}  # форма ДО #376, байт-в-байт
    for text in ("Что у меня в списке кино", "покажи задачи", "добавь молоко в покупки",
                 "какая погода", "составь покупки из меню"):
        pol = compute_unified_policy(text, _rd(text))
        assert set(pol.keys()) == GOLDEN_POLICY_KEYS, f"policy-ключи изменились при None: {text!r}"
        assert set(pol["signals"].keys()) == GOLDEN_SIGNAL_KEYS, \
            f"signals-ключи изменились при None: {text!r}"


def test_disambig_protects_other_group_domain_376():
    """CR R1 sol MAJOR: домен, поднятый ДРУГОЙ кюс-группой, НЕ вычитается.
    «список кино и покупки»: группы {checklists,shopping} и {shopping} — вердикт
    checklists не снимает shopping (он затребован второй группой явно)."""
    text = "Что у меня в списке кино и покупки"
    pol = _pol(text, DomainClassResult(("checklists",), "high"))
    assert "checklists" in pol["allowed_read"]
    assert "shopping" in pol["allowed_read"], "shopping второй группы защищён от вычитания"


# ─────────────────── проводка: флаг-гейт, дедуп нотификаций, fail-safe ───────────────────


def _flag376(monkeypatch, enabled: bool, tenants: str | None):
    import sreda.config.settings as sm
    monkeypatch.setenv("SREDA_DOMAIN_CLF_DISAMBIG", "1" if enabled else "0")
    if tenants is None:
        monkeypatch.delenv("SREDA_DOMAIN_CLF_DISAMBIG_TENANTS", raising=False)
    else:
        monkeypatch.setenv("SREDA_DOMAIN_CLF_DISAMBIG_TENANTS", tenants)
    sm.get_settings.cache_clear()


def test_gate_off_by_default_376(monkeypatch):
    """Дефолт: флаг OFF → никому (байт-в-байт текущее поведение)."""
    from sreda.runtime.react_loop import _domain_clf_disambig_for
    _flag376(monkeypatch, False, None)
    assert _domain_clf_disambig_for("tenant_max_40921122") is False


def test_gate_on_allowlist_376(monkeypatch):
    """ON + тенант в списке → True; не в списке → False; `*` → все."""
    from sreda.runtime.react_loop import _domain_clf_disambig_for
    _flag376(monkeypatch, True, "tenant_max_40921122")
    assert _domain_clf_disambig_for("tenant_max_40921122") is True
    assert _domain_clf_disambig_for("tenant_tg_777") is False
    _flag376(monkeypatch, True, "*")
    assert _domain_clf_disambig_for("tenant_tg_777") is True


def test_gate_tenants_without_flag_376(monkeypatch):
    """Список без флага → OFF (обе ручки обязательны, как #285)."""
    from sreda.runtime.react_loop import _domain_clf_disambig_for
    _flag376(monkeypatch, False, "*")
    assert _domain_clf_disambig_for("tenant_max_40921122") is False


# #410 (2026-07-21): тесты per-turn нотификации расхождения (dedup / не-роняет-ход /
# глотание сбоя доставки) УДАЛЕНЫ вместе с самим механизмом — расхождения больше не
# шлются на каждом ходе, а копятся в трейсе и уходят недельным разбором. Поведение
# «нотификатора больше нет» и агрегация разбора — tests/unit/test_410_domain_divergence_digest.py.


# ─────────────────── слой-2 (#376): сужение items-vs-overview внутри чек-листов ───────────────────


def test_narrow_excludes_overview_tool_376():
    """Слой-2: exclude_read вырезает list_checklists из read-набора, show_checklist остаётся."""
    from langchain_core.tools import StructuredTool
    from sreda.runtime.react_loop import _apply_unified_policy

    def _f(**kw):
        return "ok"
    tools = [StructuredTool.from_function(name=n, func=_f, description="t",
                                          args_schema=None, infer_schema=True)
             for n in ("show_checklist", "list_checklists")]
    out = _apply_unified_policy(tools, ["checklists", "web"], [],
                                exclude_read=frozenset({"list_checklists"}))
    names = [t.name for t in out]
    assert "show_checklist" in names
    assert "list_checklists" not in names


def test_narrow_exclude_never_touches_write_376():
    """Слой-2: exclude_read НЕ вырезает write-инструменты (защита: сужение только чтения)."""
    from langchain_core.tools import StructuredTool
    from sreda.runtime.react_loop import _apply_unified_policy

    def _f(**kw):
        return "ok"
    tools = [StructuredTool.from_function(name="add_checklist_items", func=_f, description="t",
                                          args_schema=None, infer_schema=True)]
    out = _apply_unified_policy(tools, ["checklists"], ["checklists"],
                                exclude_read=frozenset({"add_checklist_items"}))
    assert [t.name for t in out], "write-инструмент не должен вырезаться exclude_read"


def test_narrow_default_empty_byte_identical_376():
    """Слой-2: exclude_read по умолчанию пуст → фильтр байт-в-байт прежний."""
    from langchain_core.tools import StructuredTool
    from sreda.runtime.react_loop import _apply_unified_policy

    def _f(**kw):
        return "ok"
    tools = [StructuredTool.from_function(name=n, func=_f, description="t",
                                          args_schema=None, infer_schema=True)
             for n in ("show_checklist", "list_checklists")]
    base = [t.name for t in _apply_unified_policy(tools, ["checklists"], [])]
    assert base == ["show_checklist", "list_checklists"]


def test_narrow_l2r1_overview_and_write_phrases_376():
    """L2-R1 (sol/terra MAJOR): mixed/overview/write-фразы НЕ дают items+high
    (детектор чинён: окно overview {0,4}, write-основы внеси/впиши/зафиксируй)."""
    from sreda.runtime.react_preflight import classify_checklist_query as C
    r = C("а какие ещё у меня списки?")
    assert r is not None and r.kind == "overview", "«какие ещё у меня списки» → обзор"
    for t in ("внеси в список кино молоко", "впиши хлеб в список кино",
              "зафиксируй в списке кино"):
        assert C(t) is None, f"write-shaped {t!r} должен давать None"


def test_one_clause_guard_l2r2_376():
    """L2-R2 (sol/terra MAJOR): гард режет союзы или|а|но и ВНУТРЕННЮЮ пунктуацию;
    хвостовые ./!/?/… легитимны; главный кейс проходит."""
    from sreda.runtime.react_loop import _one_clause_376 as g
    assert g("Что у меня в списке кино")
    assert g("Что у меня в списке кино?")          # хвостовой знак — не разделитель
    assert not g("в списке кино или какие ещё есть")
    assert not g("покажи список кино, а какие ещё")
    assert not g("покажи список кино. Какие ещё есть?")
    assert not g("кино — какие ещё есть")
    assert not g("список кино и покупки")
    assert not g("кино но покажи всё")
    assert not g("покажи список кино\nкакие ещё есть")  # L2-R3 sol: \n = граница клауз


# ─────────────────── слой-2 v2: предысполненное чтение (по решению владельца 2026-07-16) ───────────────────


def test_prebuilt_read_pair_valid_376():
    """v2: хелпер строит валидную пару «вызов+результат» — AIMessage.tool_calls[show_checklist]
    и ToolMessage с ТЕМ ЖЕ tool_call_id и результатом реального вызова инструмента."""
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.tools import StructuredTool
    from sreda.runtime.react_loop import _prebuilt_checklist_read

    def _show(list_id_or_title: str) -> str:
        return f"# Кино к просмотру ({list_id_or_title})\n[i1] X Скорпион"
    tools = [StructuredTool.from_function(name="show_checklist", func=_show, description="t")]
    pair = _prebuilt_checklist_read(tools, "checklist_abc")
    assert pair is not None
    ai, tm = pair
    assert isinstance(ai, AIMessage) and isinstance(tm, ToolMessage)
    assert ai.tool_calls and ai.tool_calls[0]["name"] == "show_checklist"
    assert ai.tool_calls[0]["id"] == tm.tool_call_id, "инвариант пары: id вызова = id результата"
    assert "Скорпион" in str(tm.content), "результат — от реального вызова инструмента"


def test_prebuilt_read_fail_open_376():
    """v2: нет show_checklist в наборе / инструмент упал → None (fail-open, без пары)."""
    from langchain_core.tools import StructuredTool
    from sreda.runtime.react_loop import _prebuilt_checklist_read
    assert _prebuilt_checklist_read([], "x") is None

    def _boom(list_id_or_title: str) -> str:
        raise RuntimeError("db down")
    tools = [StructuredTool.from_function(name="show_checklist", func=_boom, description="t")]
    assert _prebuilt_checklist_read(tools, "x") is None


def test_v2_read_request_gate_376():
    """v2-R1 (sol MAJOR): pre-exec только на ЯВНЫЙ read-запрос без отрицания —
    утверждение/отрицание/меташум не читают список невпопад."""
    from sreda.runtime.react_loop import _re376_read_marker, _re376_negation
    def ok(t):
        tl = t.lower()
        return bool(_re376_read_marker.search(tl)) and not _re376_negation.search(tl)
    assert ok("Что у меня в списке кино")            # главный кейс
    assert ok("покажи список кино")
    assert not ok("мой список кино очень длинный")   # утверждение — нет read-маркера
    assert not ok("не показывай мой список кино")    # отрицание
    assert not ok("список кино длинноват")           # нет маркера


def test_v2r2_declarative_not_read_376():
    """v2-R2 (terra MAJOR): «я посмотрел список кино» — декларатив (ход отметки), НЕ read-запрос."""
    from sreda.runtime.react_loop import _re376_read_marker
    assert not _re376_read_marker.search("я посмотрел список кино")
    assert _re376_read_marker.search("что у меня в списке кино")  # главный кейс жив


def test_v2r2_pre_exec_in_trace_376():
    """v2-R2 (terra MAJOR): pre_exec из artifact доезжает до tool_calls_json."""
    from langchain_core.messages import AIMessage, ToolMessage
    from sreda.runtime.react_trace_persist import collect_tool_calls
    cid = "pre376_test"
    msgs = [AIMessage(content="", tool_calls=[{"name": "show_checklist",
                                               "args": {"list_id_or_title": "x"},
                                               "id": cid, "type": "tool_call"}]),
            ToolMessage(content="# X", name="show_checklist", tool_call_id=cid,
                        artifact={"result_kind": "ok", "pre_exec": True})]
    entries = collect_tool_calls(msgs, tenant_id="t")
    assert any(e.get("pre_exec") for e in entries), "pre_exec потерян в трейсе"


def test_v2r2_infinitive_not_read_376():
    """v2-R2 (sol MAJOR, тот же класс): инфинитивы/декларативы не read-запрос."""
    from sreda.runtime.react_loop import _re376_read_marker
    for t in ("я хотел показать список кино", "я посмотрю список кино",
              "я посмотрел список кино"):
        assert not _re376_read_marker.search(t), t
    assert _re376_read_marker.search("покажи список кино")
    assert _re376_read_marker.search("что у меня в списке кино")


def test_v2_sec_suppressed_on_pre_exec_376():
    """v2 (прод 16.07 18:37): на pre-exec ходе секц-директива подавлена — иначе она уходит
    отдельной user-репликой ПОСЛЕ результата, и модель отвечает ей («Поняла, буду опираться…»).
    Детект: pre_exec-ToolMessage в дельте текущего хода (сканирование до последнего Human)."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    # CR terra: импортируем БОЕВОЙ сканер (дубль логики в тесте дрейфует)
    from sreda.runtime.react_loop import _pre_exec_in_turn_376 as pre_exec_in_turn
    pair_turn = [HumanMessage(content="Что у меня в списке кино"),
                 AIMessage(content="", tool_calls=[{"name": "show_checklist",
                                                    "args": {}, "id": "pre376_x",
                                                    "type": "tool_call"}]),
                 ToolMessage(content="# X", name="show_checklist", tool_call_id="pre376_x",
                             artifact={"result_kind": "ok", "pre_exec": True})]
    assert pre_exec_in_turn(pair_turn)
    plain_turn = [HumanMessage(content="Что у меня в списке кино")]
    assert not pre_exec_in_turn(plain_turn)
    # pre_exec ПРОШЛОГО хода (за Human) — не считается
    old_pre = [ToolMessage(content="# X", name="show_checklist", tool_call_id="old",
                           artifact={"pre_exec": True}),
               HumanMessage(content="новый вопрос")]
    assert not pre_exec_in_turn(old_pre)


def test_glue_tail_to_last_human_376():
    """v2-root (sol/субагент MINOR): сплайс хвоста — длина сохранена, пара замыкает,
    хвост в последнем НАСТОЯЩЕМ Human, исходные объекты не мутированы."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
    from sreda.runtime.react_loop import _glue376_tail_to_last_human
    h = HumanMessage(content="Что у меня в списке кино")
    msgs = [SystemMessage(content="sp"), h,
            AIMessage(content="", tool_calls=[{"name": "show_checklist", "args": {},
                                               "id": "pre376_t", "type": "tool_call"}]),
            ToolMessage(content="# X", name="show_checklist", tool_call_id="pre376_t")]
    out = _glue376_tail_to_last_human(msgs, "Сейчас 12:00\n\nДоступны: …")
    assert len(out) == len(msgs), "длина сохранена"
    assert isinstance(out[-1], ToolMessage) and isinstance(out[-2], AIMessage), "пара замыкает"
    assert "Сейчас 12:00" in out[1].content and "списке кино" in out[1].content, "хвост в Human"
    assert h.content == "Что у меня в списке кино", "исходный Human не мутирован"
    # Human нет вообще → прежнее поведение (trailing-user)
    out2 = _glue376_tail_to_last_human([SystemMessage(content="sp")], "tt")
    assert isinstance(out2[-1], HumanMessage) and out2[-1].content == "tt"
