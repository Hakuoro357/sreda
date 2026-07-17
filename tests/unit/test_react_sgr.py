"""#383 Ф1 — модуль SGR-шага (react_sgr): срез, wire-схема, парс, маппинг в AIMessage.

RED-до-кода по плану plans/383-sgr-final.md §7 Ф1 (vex-assistant). Пункты приёмки:
- срез sgr_tools: канонизация имён (link_task), fail-closed на неизвестных, мета вне;
- схема: без $ref, покрытие ровно среза, описания = bound-объектов, optional → nullable,
  формы flat (Mercury) и envelope (Оса) — Ф0-результат;
- схемная связка enough_data⇔kind (test_sgr_schema_couples_enough_data — приёмка п.4);
- парс: валид/невалид (чужой инструмент, битые args, native tool_calls), null-вычистка;
- маппинг act/clarify/finish → AIMessage; PII-free трейс-поля (приёмка п.10).
"""
from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from sreda.runtime.react_sgr import (
    SGR_SYSTEM_BLOCK,
    STRUCTURED_HISTORY_MODE,
    WIRE_SHAPE_BY_PROVIDER,
    SgrDecision,
    SgrInvalidReply,
    build_wire_schema,
    compute_sgr_tools,
    decision_to_aimessage,
    decision_trace_fields,
    parse_sgr_reply,
)

ALIASES = {"link_task": "link_task_to_checklist"}


# ─────────────────────────── фикстуры-инструменты (реальные имена — метаданные families) ───────────


@tool
def create_checklist(title: str) -> str:
    """Создать чек-лист с названием."""
    return "ok"


@tool
def show_checklist(name: str | None = None) -> str:
    """Показать список по имени (optional-параметр для strict-нормализации)."""
    return "ok"


class _AddItems(BaseModel):
    name: str = Field(description="имя списка")
    items: list[str] = Field(description="пункты")


@tool(args_schema=_AddItems)
def add_checklist_items(name: str, items: list[str]) -> str:
    """Добавить пункты в чек-лист (вложенная модель со ссылками в json-схеме)."""
    return "ok"


@tool
def web_search(query: str) -> str:
    """Поиск в интернете."""
    return "ok"


@tool
def get_weather(location: str, day_offset: int = 0) -> str:
    """Погода (int-параметр для strict-теста коэрции)."""
    return "ok"


@tool
def schedule_reminder(title: str) -> str:
    """Напоминание (домен reminders — ВНЕ среза)."""
    return "ok"


@tool
def link_task(task_ref: str, checklist_ref: str) -> str:
    """Runtime-имя с алиасом (канон link_task_to_checklist, домены tasks+checklists — вне среза)."""
    return "ok"


@tool
def ask_human(question: str) -> str:
    """Мета — вне среза (представлен веткой clarify)."""
    return "ok"


@tool
def totally_unknown_tool(x: str) -> str:
    """Неизвестен метаданным — fail-closed."""
    return "ok"


def _bound() -> list:
    return [create_checklist, show_checklist, add_checklist_items, web_search, get_weather,
            schedule_reminder, link_task, ask_human, totally_unknown_tool]


def _slice() -> list:
    return compute_sgr_tools(_bound(), ALIASES)


def _act_reply(action: str, args: dict, situation: str = "Пользователь просит действие.") -> str:
    return json.dumps({"kind": "act", "situation": situation,
                       "enough_data": True, "tool": {"action": action, "args": args}},
                      ensure_ascii=False)


# ─────────────────────────── срез sgr_tools ───────────────────────────


def test_sgr_tools_slice_domains_and_meta():
    names = [t.name for t in _slice()]
    assert names == ["create_checklist", "show_checklist", "add_checklist_items",
                     "web_search", "get_weather"]


def test_sgr_tools_canonicalizes_alias_without_error():
    # link_task (runtime-имя) канонизируется → link_task_to_checklist (tasks+checklists) → вне среза,
    # БЕЗ KeyError (R2 terra). Неизвестное имя — fail-closed.
    names = [t.name for t in _slice()]
    assert "link_task" not in names
    assert "totally_unknown_tool" not in names


# ─────────────────────────── wire-схема ───────────────────────────


def test_wire_schema_flat_no_ref_and_exact_coverage():
    st = _slice()
    schema = build_wire_schema(st, "flat")
    dumped = json.dumps(schema)
    assert "$ref" not in dumped and "$defs" not in dumped
    act = schema["anyOf"][0]
    branches = act["properties"]["tool"]["anyOf"]
    assert [b["properties"]["action"]["const"] for b in branches] == [t.name for t in st]
    # описания веток = описания bound-объектов (R1: не терять прод-описания)
    for b, t in zip(branches, st):
        assert b["description"] == t.description


def test_wire_schema_long_description_not_truncated():
    # CR R1 sol MINOR: инвариант равенства описаний ТОЧНЫЙ — без обрезания даже на длинных
    long_desc = "Показать список. " * 60  # ~1000 символов
    t = create_checklist.model_copy(update={"description": long_desc})
    schema = build_wire_schema([t], "flat")
    branch = schema["anyOf"][0]["properties"]["tool"]["anyOf"][0]
    assert branch["description"] == long_desc


def test_wire_schema_couples_enough_data():
    # приёмка п.4: схемная связка — act несёт const true, clarify const false, finish const true
    schema = build_wire_schema(_slice(), "flat")
    act, clarify, finish = schema["anyOf"]
    assert act["properties"]["enough_data"] == {"const": True}
    assert clarify["properties"]["enough_data"] == {"const": False}
    assert finish["properties"]["task_completed"] == {"const": True}


def test_wire_schema_optional_param_nullable():
    # show_checklist.name: str|None=None → required + nullable (anyOf с null)
    schema = build_wire_schema(_slice(), "flat")
    branches = schema["anyOf"][0]["properties"]["tool"]["anyOf"]
    show = next(b for b in branches if b["properties"]["action"]["const"] == "show_checklist")
    args = show["properties"]["args"]
    assert "name" in args["required"]
    name_schema = args["properties"]["name"]
    assert any(x.get("type") == "null" for x in name_schema["anyOf"] if isinstance(x, dict))


def test_wire_schema_envelope_shape():
    # Ф0: Groq запрещает top-level anyOf → envelope = root object {situation, step}
    schema = build_wire_schema(_slice(), "envelope")
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"situation", "step"}
    for br in schema["properties"]["step"]["anyOf"]:
        assert "situation" not in br["properties"]


def test_wire_shape_and_history_mode_fixed_by_f0():
    assert WIRE_SHAPE_BY_PROVIDER["inception-mercury2"] == "flat"
    assert WIRE_SHAPE_BY_PROVIDER["groq-gpt-oss-120b"] == "envelope"
    assert STRUCTURED_HISTORY_MODE == "plain"


def test_wire_schema_min_lengths_mirror_parser():
    # CR R2 sol MAJOR: schema-валидный ответ обязан проходить парсер — пустые строки
    # запрещены УЖЕ схемой (minLength=1 зеркалит pydantic min_length=1)
    act, clarify, finish = build_wire_schema(_slice(), "flat")["anyOf"]
    for branch in (act, clarify, finish):
        assert branch["properties"]["situation"]["minLength"] == 1
    assert clarify["properties"]["question"]["minLength"] == 1
    assert finish["properties"]["reply"]["minLength"] == 1


@pytest.mark.parametrize("payload, code", [
    ({"kind": "act", "situation": "s", "enough_data": 1,
      "tool": {"action": "create_checklist", "args": {"title": "x"}}}, "enough_data_not_bool"),
    ({"kind": "finish", "situation": "s", "task_completed": 1, "reply": "готово"},
     "task_completed_not_bool"),
])
def test_parse_rejects_int_for_bool_literal(payload, code):
    # CR R3 sol MAJOR: pydantic Literal[True] принимает 1 даже в strict (hash-коллизия) —
    # явный тип-гейт обязан отвергать (wire-схема требует const true/false)
    with pytest.raises(SgrInvalidReply) as ei:
        parse_sgr_reply(json.dumps(payload, ensure_ascii=False), None, _slice())
    assert code in str(ei.value)


def test_parse_title_object_coercion_parity_with_dispatch():
    # CR R3 sol, суб-претензия B — ОТКЛОНЕНА осознанно (решение зафиксировано этим тестом):
    # прод-схемы инструментов НАМЕРЕННО нормализуют {"title": str} → str (#124,
    # BeforeValidator в specs_checklists) — тот же валидатор отработает на dispatch в
    # run_tools. SGR-парс ПРИНИМАЕТ эту форму = паритет с легаси-путём; отвергать = быть
    # строже прода и бессмысленно бегать в легаси, который исполнит то же самое.
    from sreda.services.tool_schemas.specs_checklists import AddChecklistItemsInput
    prod_tool = create_checklist.model_copy(
        update={"name": "add_checklist_items", "args_schema": AddChecklistItemsInput})
    d = parse_sgr_reply(_act_reply("add_checklist_items",
                                   {"list_id_or_title": "продукты",
                                    "items": [{"title": "молоко"}]}),
                        None, [prod_tool])
    assert d.action == "add_checklist_items"  # приняли — как принял бы dispatch


def test_parse_rejects_numeric_string_coercion():
    # CR R2 terra MAJOR: строгая валидация без коэрции — "1" вместо int не проходит
    # (иначе сырое "1" уехало бы в run_tools при schema-невалидном типе)
    with pytest.raises(SgrInvalidReply) as ei:
        parse_sgr_reply(_act_reply("get_weather", {"location": "Москва", "day_offset": "1"}),
                        None, _slice())
    assert "args_invalid" in str(ei.value)
    d = parse_sgr_reply(_act_reply("get_weather", {"location": "Москва", "day_offset": 1}),
                        None, _slice())
    assert d.args == {"location": "Москва", "day_offset": 1}


# ─────────────────────────── парс ───────────────────────────


def test_parse_valid_act_flat():
    d = parse_sgr_reply(_act_reply("create_checklist", {"title": "Ремонт"}), None, _slice())
    assert isinstance(d, SgrDecision)
    assert (d.kind, d.action, d.args) == ("act", "create_checklist", {"title": "Ремонт"})
    assert d.enough_data is True


def test_parse_valid_act_envelope_normalized():
    raw = json.dumps({"situation": "Просьба показать список.",
                      "step": {"kind": "act", "enough_data": True,
                               "tool": {"action": "show_checklist", "args": {"name": "продукты"}}}},
                     ensure_ascii=False)
    d = parse_sgr_reply(raw, None, _slice())
    assert (d.kind, d.action) == ("act", "show_checklist")


def test_parse_envelope_inner_situation_rejected():
    # CR R1 sol+terra MAJOR: situation ВНУТРИ step запрещён схемой конверта — внутренний НЕ
    # должен маскировать невалидный корневой (fail-closed, не «внутренний перезаписал»)
    raw = json.dumps({"situation": 123,  # невалидный корень
                      "step": {"kind": "clarify", "situation": "маскирующий валидный",
                               "enough_data": False, "question": "Какой список?"}},
                     ensure_ascii=False)
    with pytest.raises(SgrInvalidReply):
        parse_sgr_reply(raw, None, _slice())
    raw2 = json.dumps({"situation": "валидный корень",
                       "step": {"kind": "clarify", "situation": "лишний внутренний",
                                "enough_data": False, "question": "Какой список?"}},
                      ensure_ascii=False)
    with pytest.raises(SgrInvalidReply) as ei:
        parse_sgr_reply(raw2, None, _slice())
    assert "envelope_inner_situation" in str(ei.value)


def test_parse_act_null_args_stripped_before_validation():
    # null-вычистка §3: optional-поле пришло null → удаляется, args_schema проходит
    d = parse_sgr_reply(_act_reply("show_checklist", {"name": None}), None, _slice())
    assert d.args == {}


def test_parse_clarify_and_finish():
    dc = parse_sgr_reply(json.dumps({"kind": "clarify", "situation": "Не назван список.",
                                     "enough_data": False, "question": "В какой список добавить?"},
                                    ensure_ascii=False), None, _slice())
    assert (dc.kind, dc.question, dc.enough_data) == ("clarify", "В какой список добавить?", False)
    df = parse_sgr_reply(json.dumps({"kind": "finish", "situation": "Список показан.",
                                     "task_completed": True, "reply": "Вот твой список."},
                                    ensure_ascii=False), None, _slice())
    assert (df.kind, df.reply, df.task_completed) == ("finish", "Вот твой список.", True)


@pytest.mark.parametrize("raw, reason", [
    ("не json", "json_error"),
    (json.dumps({"kind": "act", "situation": "s", "enough_data": False,
                 "tool": {"action": "create_checklist", "args": {"title": "x"}}}),
     "branch"),  # act при enough_data=false — связка нарушена (приёмка п.4, код-бекстоп)
    (_act_reply("schedule_reminder", {"title": "x"}), "unknown_tool"),  # инструмент вне среза
    (_act_reply("create_checklist", {}), "args_invalid"),               # обязательный title отсутствует
    (_act_reply("create_checklist", {"title": "x", "extra": 1}), "args_invalid"),
    (json.dumps({"kind": "finish", "situation": "s", "task_completed": True, "reply": ""}), "branch"),
])
def test_parse_invalid_variants(raw, reason):
    with pytest.raises(SgrInvalidReply) as ei:
        parse_sgr_reply(raw, None, _slice())
    assert reason in str(ei.value)


def test_parse_rejects_native_tool_calls():
    tc = [{"name": "create_checklist", "args": {"title": "x"}, "id": "abc", "type": "tool_call"}]
    with pytest.raises(SgrInvalidReply) as ei:
        parse_sgr_reply(_act_reply("create_checklist", {"title": "x"}), tc, _slice())
    assert "native_tool_calls" in str(ei.value)


def test_parse_no_partial_acceptance():
    # g-025: невалид не возвращает "почти распарсенное" — только исключение
    with pytest.raises(SgrInvalidReply):
        parse_sgr_reply(json.dumps({"kind": "act", "situation": "s", "enough_data": True,
                                    "tool": {"action": "create_checklist"}}), None, _slice())


# ─────────────────────────── маппинг в AIMessage ───────────────────────────


def test_decision_to_aimessage_act():
    d = parse_sgr_reply(_act_reply("add_checklist_items",
                                   {"name": "продукты", "items": ["молоко"]}), None, _slice())
    msg = decision_to_aimessage(d)
    assert isinstance(msg, AIMessage) and msg.content == ""
    (tc,) = msg.tool_calls
    assert tc["name"] == "add_checklist_items"
    assert tc["args"] == {"name": "продукты", "items": ["молоко"]}
    assert tc["id"].startswith("sgr-")


def test_decision_to_aimessage_clarify_maps_to_ask_human():
    d = parse_sgr_reply(json.dumps({"kind": "clarify", "situation": "s",
                                    "enough_data": False, "question": "Какой список?"},
                                   ensure_ascii=False), None, _slice())
    msg = decision_to_aimessage(d)
    (tc,) = msg.tool_calls
    assert tc["name"] == "ask_human"
    assert tc["args"] == {"question": "Какой список?"}


def test_decision_to_aimessage_finish_plain_text():
    d = parse_sgr_reply(json.dumps({"kind": "finish", "situation": "s",
                                    "task_completed": True, "reply": "Готово, добавила."},
                                   ensure_ascii=False), None, _slice())
    msg = decision_to_aimessage(d)
    assert msg.content == "Готово, добавила." and not msg.tool_calls


# ─────────────────────────── PII-free трейс (приёмка п.10) ───────────────────────────


def test_sgr_trace_no_free_text():
    d = parse_sgr_reply(_act_reply("create_checklist", {"title": "Секретный список"},
                                   situation="Пользователь хочет секретный список."), None, _slice())
    fields = decision_trace_fields(d, tenant_id="tenant-a")
    assert set(fields) <= {"kind", "action", "enough_data", "task_completed",
                           "situation_len", "args_hash"}
    dumped = json.dumps(fields, ensure_ascii=False)
    assert "Секретный" not in dumped and "секретный" not in dumped
    assert fields["situation_len"] == len("Пользователь хочет секретный список.")
    assert isinstance(fields["args_hash"], str) and len(fields["args_hash"]) >= 8


def test_sgr_trace_args_hash_is_keyed_hmac_not_plain_sha():
    # CR R2 sol MAJOR: низкоэнтропийные args («молоко») нельзя перебирать словарём —
    # keyed HMAC трейса #192 (v1:key_id:digest), НЕ голый sha; tenant-separation
    import hashlib as _hl
    d = parse_sgr_reply(_act_reply("add_checklist_items",
                                   {"name": "продукты", "items": ["молоко"]}), None, _slice())
    h_a = decision_trace_fields(d, tenant_id="tenant-a")["args_hash"]
    assert h_a.startswith("v1:")
    plain = _hl.sha256(json.dumps(d.args, sort_keys=True, ensure_ascii=False,
                                  separators=(",", ":")).encode("utf-8")).hexdigest()
    assert plain not in h_a
    assert _hl.sha1(json.dumps(d.args, sort_keys=True,
                               ensure_ascii=False).encode("utf-8")).hexdigest()[:16] not in h_a
    h_b = decision_trace_fields(d, tenant_id="tenant-b")["args_hash"]
    assert h_a != h_b  # tenant-separation против кросс-тенантной корреляции
    assert h_a == decision_trace_fields(d, tenant_id="tenant-a")["args_hash"]  # детерминизм


def test_sgr_trace_hash_fail_safe_without_key(monkeypatch):
    # нет ключа шифрования → args_hash=None (сводка не роняет ход и не течёт сырьём)
    from sreda.services import trace_hash as th
    monkeypatch.setattr(th, "_derive_key",
                        lambda: (_ for _ in ()).throw(ValueError("no key")))
    d = parse_sgr_reply(_act_reply("create_checklist", {"title": "x"}), None, _slice())
    fields = decision_trace_fields(d, tenant_id="t")
    assert fields["args_hash"] is None
    assert "x" not in json.dumps({k: v for k, v in fields.items() if k != "kind"})


def test_sgr_trace_clarify_question_not_leaked():
    d = parse_sgr_reply(json.dumps({"kind": "clarify", "situation": "s", "enough_data": False,
                                    "question": "Приватный вопрос?"}, ensure_ascii=False),
                        None, _slice())
    assert "Приватный" not in json.dumps(decision_trace_fields(d, tenant_id="t"),
                                         ensure_ascii=False)


def test_sgr_system_block_is_stable_russian_text():
    assert isinstance(SGR_SYSTEM_BLOCK, str) and "clarify" in SGR_SYSTEM_BLOCK
    assert "анкет" in SGR_SYSTEM_BLOCK.lower()
