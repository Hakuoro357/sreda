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


def test_notify_dedup_same_key_once_376(monkeypatch):
    """Одинаковое расхождение (тот же кортеж) в TTL-окне → ОДИН алерт; другое → проходит."""
    import asyncio as _a
    from sreda.runtime import react_loop as rl
    sent: list = []

    async def main():
        rl._DIS376_SEEN.clear()
        real_create = _a.create_task

        def _capture(coro, **kw):
            sent.append(coro)
            return real_create(_noop())
        monkeypatch.setattr(rl.asyncio, "create_task", _capture)
        dis1 = {"static_domains": ["checklists", "shopping"], "freddie_domains": ["checklists"],
                "kind": "subtract", "applied": True}
        rl._notify_domain_divergence("tenant_x", dis1)
        rl._notify_domain_divergence("tenant_x", dis1)          # дубль → не шлём
        dis2 = {**dis1, "kind": "add", "applied": False}         # другой вид → шлём
        rl._notify_domain_divergence("tenant_x", dis2)

    async def _noop():
        return None

    _a.run(main())
    for c in sent:  # закрыть неисполненные корутины (анти-warning)
        c.close()
    assert len(sent) == 2, f"ожидали 2 алерта (уникальные ключи), получили {len(sent)}"


def test_notify_never_raises_376(monkeypatch):
    """Сбой доставки/окружения НИКОГДА не роняет ход (fire-and-forget, глотаем)."""
    from sreda.runtime import react_loop as rl
    rl._DIS376_SEEN.clear()
    # вне event loop create_task кинет RuntimeError → общий except должен проглотить
    rl._notify_domain_divergence("tenant_x", {"static_domains": ["a"], "freddie_domains": ["b"],
                                              "kind": "add", "applied": False})  # не должно raise


def test_send_alert_swallows_376(monkeypatch):
    """_dis376_send_alert глотает исключение доставки."""
    import asyncio as _a
    from sreda.runtime import react_loop as rl

    async def _boom(text):
        raise RuntimeError("down")
    import sreda.services.admin_alerts as aa
    monkeypatch.setattr(aa, "alert_admin_async", _boom)
    _a.run(rl._dis376_send_alert("test"))  # не должно raise


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
    assert not g("покажи список кино
какие ещё есть")  # L2-R3 sol: 
 = граница клауз
