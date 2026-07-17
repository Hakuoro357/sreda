"""#383 SGR-шаг планировщика (Schema-Guided Reasoning) — домен чеклистов, за флагом.

Чистые функции шага планировщика со схемным принуждением (план vex-assistant
plans/383-sgr-final.md): вместо свободного function calling модель отвечает строгим
json_schema — анкета рассуждения (situation / enough_data / task_completed) + выбор РОВНО
одного действия из размеченного объединения доменного среза инструментов (sgr_tools),
либо clarify (→ ask_human), либо finish (финальный текст).

Модуль НЕ импортирует react_loop (иначе цикл: react_loop lazy-импортирует нас внутри
SGR-гейта) — алиасы имён и наборы передаются параметрами. Ничего не мутирует и не ходит
в сеть/БД: вызов LLM остаётся в chat-узле react_loop (Ф2).

Ф0-результаты (2026-07-17, scripts/probe_sgr_383.py, гейт PASS на обоих провайдерах):
- ``STRUCTURED_HISTORY_MODE = "plain"`` — истории с AIMessage.tool_calls + ToolMessage
  оба провайдера принимают БЕЗ ``tools=`` (contingency tools_none не понадобился);
- ``WIRE_SHAPE_BY_PROVIDER`` — форма схемы ПЕР-ПРОВАЙДЕР: Mercury «flat» (двухуровневый
  конверт guided-режим ронял в ~15%), Оса/Groq «envelope» (их валидатор запрещает anyOf
  на верхнем уровне; constrained decoding конверт держит без единого дропа);
- пустые args-объекты — БЕЗ ключа ``required`` (Groq: 400 на required при пустых properties).
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from langchain_core.messages import AIMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Домены среза (§3 плана): только чеклисты + web; мета представлена веткой clarify.
SGR_DOMAINS = frozenset({"checklists", "web"})
_EXCLUDED_META = frozenset({"ask_human", "need_family", "delete_my_account"})

# ── Ф0-константы (не менять без новой пробы: контракт зафиксирован гейтом Ф0) ──
STRUCTURED_HISTORY_MODE: Literal["plain", "tools_none"] = "plain"
WIRE_SHAPE_BY_PROVIDER: dict[str, str] = {
    "inception-mercury2": "flat",
    "groq-gpt-oss-120b": "envelope",
}

# Стабильный SGR-блок системного промпта (кеш-дисциплина: текст статичен; g-075 — clarify
# обязан быть человеческим). Семантика анкеты — зеркало wire-схемы.
SGR_SYSTEM_BLOCK = (
    "Сейчас ты работаешь с разделом «Списки дел» (чек-листы) в режиме анкеты.\n"
    "Отвечай ТОЛЬКО объектом по заданной схеме — анкетой шага:\n"
    "- situation: 1-2 фразы — что просит пользователь и что уже сделано в этом ходе.\n"
    "- РОВНО ОДНО из трёх:\n"
    "  * act — данных достаточно: выбери ОДИН инструмент из списка и заполни его аргументы\n"
    "    строго по их смыслу (описания инструментов — в схеме). Не выдумывай названия списков\n"
    "    и пунктов, которых пользователь не называл.\n"
    "  * clarify — данных НЕ хватает (не назван список, пункт или само действие неоднозначно):\n"
    "    задай короткий человеческий вопрос. Никаких технических слов.\n"
    "  * finish — задача пользователя уже выполнена (результат виден в истории) или ход чисто\n"
    "    разговорный: дай финальный ответ пользователю. Не утверждай, что сделала действие,\n"
    "    которого не было в истории.\n"
    "Если в истории есть результат инструмента — опирайся на него, не перечитывай заново без нужды."
)


class SgrInvalidReply(ValueError):
    """Невалидный structured-ответ: reason-код в args[0] (json_error | top_type | branch |
    unknown_tool | args_invalid | native_tool_calls). ЛЮБОЙ невалид → вызывающий уходит в
    легаси тем же проходом (§5 плана); частичное принятие запрещено (g-025)."""


# ─────────────────────────── срез sgr_tools ───────────────────────────


def compute_sgr_tools(tools: list, aliases: Mapping[str, str]) -> list:
    """Доменный срез фактического bound (§3): канонизация runtime-имени через aliases
    (R2 terra: в bound живёт ``link_task``, метаданные знают ``link_task_to_checklist``) →
    read∪write домены ⊆ {checklists, web}. Неизвестное после канонизации имя — fail-closed
    (исключается молча). Мета — вне (clarify/need_family/self-delete не ветки действия).
    Confirm-обёрнутые кандидаты проходят наравне (обёртка сохраняет name/args_schema)."""
    from sreda.services.tool_schemas.families import (
        TOOL_OP_CLASS, tool_read_domains, tool_write_domains,
    )
    out = []
    for t in tools:
        if t.name in _EXCLUDED_META:
            continue
        canon = aliases.get(t.name, t.name)
        if canon not in TOOL_OP_CLASS:
            continue  # fail-closed: метаданные не знают инструмент
        doms = set(tool_read_domains(canon)) | set(tool_write_domains(canon))
        if doms and doms <= SGR_DOMAINS:
            out.append(t)
    return out


# ─────────────────────────── wire-схема ───────────────────────────


def _inline_defs(node: Any, defs: dict) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            name = str(node["$ref"]).split("/")[-1]
            target = copy.deepcopy(defs.get(name, {}))
            target.update({k: v for k, v in node.items() if k != "$ref"})
            return _inline_defs(target, defs)
        return {k: _inline_defs(v, defs) for k, v in node.items()
                if k not in ("$defs", "definitions")}
    if isinstance(node, list):
        return [_inline_defs(x, defs) for x in node]
    return node


def _strict_normalize(schema: dict) -> dict:
    """Рекурсивно к strict-совместимой форме (R2 terra): инлайн $defs (без $ref в выхлопе);
    additionalProperties:false; ВСЕ properties → required, при этом optional-поле становится
    nullable (anyOf [тип, null]) — обратная null-вычистка в parse_sgr_reply. Пустые
    properties — БЕЗ ключа required (Ф0: Groq 400 иначе)."""
    defs = {**schema.get("$defs", {}), **schema.get("definitions", {})}
    node = _inline_defs(copy.deepcopy(schema), defs)

    def walk(n: Any) -> Any:
        if isinstance(n, list):
            return [walk(x) for x in n]
        if not isinstance(n, dict):
            return n
        n = {k: walk(v) for k, v in n.items()}
        if n.get("type") == "object" and "properties" in n:
            n["additionalProperties"] = False
            prev_req = set(n.get("required", []))
            props = n["properties"]
            for pname, pschema in list(props.items()):
                if pname not in prev_req and not (
                        isinstance(pschema, dict) and isinstance(pschema.get("anyOf"), list)
                        and any(isinstance(b, dict) and b.get("type") == "null"
                                for b in pschema["anyOf"])):
                    props[pname] = {"anyOf": [pschema, {"type": "null"}]}
            if props:
                n["required"] = sorted(props.keys())
            else:
                n.pop("required", None)
        return n

    return walk(node)


def _tool_branches(sgr_tools: list) -> list[dict]:
    """Ветка на инструмент: const-дискриминатор action + args из штатного LangChain-конвертера
    (нормализованные parameters) + ОПИСАНИЕ bound-объекта БЕЗ обрезания (CR R1 sol MINOR:
    инвариант «описание ветки = описание bound» точный — близкие get/show/list различимы
    только по описаниям, те же _react_desc, что видит легаси-путь)."""
    from langchain_core.utils.function_calling import convert_to_openai_tool
    branches = []
    for t in sgr_tools:
        fn = convert_to_openai_tool(t)["function"]
        params = _strict_normalize(fn.get("parameters") or {"type": "object", "properties": {}})
        if params.get("type") != "object":
            params = {"type": "object", "properties": {}, "additionalProperties": False}
        branches.append({
            "type": "object", "additionalProperties": False,
            "description": fn.get("description") or "",
            "required": ["action", "args"],
            "properties": {"action": {"const": fn["name"]}, "args": params},
        })
    return branches


_SITUATION = {"type": "string", "maxLength": 400}


def build_wire_schema(sgr_tools: list, shape: str) -> dict:
    """Wire-схема анкеты в форме ПЕР-ПРОВАЙДЕР (Ф0-поправка §3, см. шапку модуля).
    Связка enough_data⇔kind — СХЕМОЙ (const-поля per-branch, #180: гейт механизмом):
    ветка act несёт enough_data:const true — «действовать при нехватке данных» синтаксически
    невыразимо; clarify — const false; finish — task_completed const true."""
    flat = {"anyOf": [
        {"type": "object", "additionalProperties": False,
         "required": ["kind", "situation", "enough_data", "tool"],
         "properties": {
             "kind": {"const": "act"},
             "situation": _SITUATION,
             "enough_data": {"const": True},
             "tool": {"anyOf": _tool_branches(sgr_tools)},
         }},
        {"type": "object", "additionalProperties": False,
         "required": ["kind", "situation", "enough_data", "question"],
         "properties": {
             "kind": {"const": "clarify"},
             "situation": _SITUATION,
             "enough_data": {"const": False},
             "question": {"type": "string", "maxLength": 300},
         }},
        {"type": "object", "additionalProperties": False,
         "required": ["kind", "situation", "task_completed", "reply"],
         "properties": {
             "kind": {"const": "finish"},
             "situation": _SITUATION,
             "task_completed": {"const": True},
             "reply": {"type": "string"},
         }},
    ]}
    if shape == "flat":
        return flat
    if shape == "envelope":
        step_branches = []
        for br in flat["anyOf"]:
            b = copy.deepcopy(br)
            b["properties"].pop("situation", None)
            b["required"] = [k for k in b["required"] if k != "situation"]
            step_branches.append(b)
        return {
            "type": "object", "additionalProperties": False,
            "required": ["situation", "step"],
            "properties": {"situation": _SITUATION, "step": {"anyOf": step_branches}},
        }
    raise ValueError(f"unknown wire shape: {shape!r}")


# ─────────────────────────── парс ответа (pydantic, discriminated union) ───────────────────────────


class _ToolChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    args: dict


class _ActStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["act"]
    situation: str = Field(min_length=1, max_length=400)
    enough_data: Literal[True]
    tool: _ToolChoice


class _ClarifyStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["clarify"]
    situation: str = Field(min_length=1, max_length=400)
    enough_data: Literal[False]
    question: str = Field(min_length=1, max_length=300)


class _FinishStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["finish"]
    situation: str = Field(min_length=1, max_length=400)
    task_completed: Literal[True]
    reply: str = Field(min_length=1)


class _AnyStep(BaseModel):
    root: _ActStep | _ClarifyStep | _FinishStep = Field(discriminator="kind")


@dataclass(frozen=True)
class SgrDecision:
    """Распарсенное решение SGR-шага (истина для маппинга и трейса)."""
    kind: Literal["act", "clarify", "finish"]
    situation: str
    enough_data: bool | None = None
    task_completed: bool | None = None
    action: str | None = None
    args: dict | None = None
    question: str | None = None
    reply: str | None = None


def parse_sgr_reply(content: str, tool_calls: Any, sgr_tools: list) -> SgrDecision:
    """Строгий парс structured-ответа. ЛЮБОЙ невалид → SgrInvalidReply (reason-код) —
    вызывающий деградирует в легаси (§5); частичного принятия нет (g-025).

    content: тело ответа (JSON-текст). tool_calls: native tool_calls ответа (возможны в
    contingency-режиме tools_none) — их наличие = невалид (SGR-ответ обязан быть анкетой).
    sgr_tools: срез ЭТОГО прохода — действие обязано быть из него; args после null-вычистки
    валидируются args_schema самого инструмента."""
    if tool_calls:
        raise SgrInvalidReply("native_tool_calls")
    try:
        data = json.loads(content)
    except Exception as e:  # noqa: BLE001 — любой парс-сбой = один reason-код
        raise SgrInvalidReply(f"json_error:{type(e).__name__}") from None
    if not isinstance(data, dict):
        raise SgrInvalidReply("top_type")
    # Нормализация конверт → плоская (Ф0: у Осы форма envelope). Fail-closed (CR R1 sol+terra
    # MAJOR): «situation» ВНУТРИ step запрещён схемой конверта — не даём внутреннему значению
    # перезаписать/замаскировать корневое (невалидный корень прошёл бы). Корневое — последним.
    if set(data.keys()) == {"situation", "step"} and isinstance(data.get("step"), dict):
        if "situation" in data["step"]:
            raise SgrInvalidReply("branch:envelope_inner_situation")
        data = {**data["step"], "situation": data["situation"]}
    try:
        step = _AnyStep(root=data).root
    except ValidationError as e:
        raise SgrInvalidReply(f"branch:{e.error_count()}") from None
    if isinstance(step, _ActStep):
        if not any(t.name == step.tool.action for t in sgr_tools):
            raise SgrInvalidReply(f"unknown_tool:{step.tool.action}")
        args = {k: v for k, v in step.tool.args.items() if v is not None}  # null-вычистка §3
        t = next(t for t in sgr_tools if t.name == step.tool.action)
        try:
            # Ключи строго ⊆ схеме инструмента: модели инструментов терпимы к extra
            # (pydantic ignore), а run_tools передаёт args как kwargs — лишний ключ
            # взорвался бы только на dispatch. Ловим на парсе (fail-closed).
            allowed = set(t.args_schema.model_json_schema().get("properties", {}))
            extra = set(args) - allowed
            if extra:
                raise SgrInvalidReply(f"args_invalid:extra_keys:{sorted(extra)}")
            t.args_schema.model_validate(args)
        except SgrInvalidReply:
            raise
        except Exception as e:  # noqa: BLE001 — pydantic/args любого вида
            raise SgrInvalidReply(f"args_invalid:{type(e).__name__}") from None
        return SgrDecision(kind="act", situation=step.situation, enough_data=True,
                           action=step.tool.action, args=args)
    if isinstance(step, _ClarifyStep):
        return SgrDecision(kind="clarify", situation=step.situation, enough_data=False,
                           question=step.question)
    return SgrDecision(kind="finish", situation=step.situation, task_completed=True,
                       reply=step.reply)


# ─────────────────────────── маппинг и трейс ───────────────────────────


def decision_to_aimessage(d: SgrDecision) -> AIMessage:
    """SgrDecision → обычный AIMessage: даунстрим (run_tools, confirm, паузы, route) не
    отличает SGR-ход от легаси. act → один tool_call (id ``sgr-<uuid>``); clarify →
    tool_call ask_human (существующий interrupt-механизм пауз); finish → текст → END."""
    if d.kind == "act":
        return AIMessage(content="", tool_calls=[{
            "name": d.action, "args": dict(d.args or {}),
            "id": f"sgr-{uuid.uuid4().hex[:12]}", "type": "tool_call"}])
    if d.kind == "clarify":
        return AIMessage(content="", tool_calls=[{
            "name": "ask_human", "args": {"question": d.question},
            "id": f"sgr-{uuid.uuid4().hex[:12]}", "type": "tool_call"}])
    return AIMessage(content=d.reply or "")


def decision_trace_fields(d: SgrDecision) -> dict:
    """PII-free сводка для поля ``sgr`` в llm_calls (приёмка п.10, g-075/g-039): только
    enum/bool/числа/hash — НИКАКОГО свободного текста (situation/question/reply/args не
    попадают; admin/trace_parser рендерит llm_calls-поля как безопасные). Детекция петель
    #267 — по повтору (kind, action, args_hash) между проходами."""
    args_hash = hashlib.sha1(
        json.dumps(d.args or {}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "kind": d.kind,
        "action": d.action,
        "enough_data": d.enough_data,
        "task_completed": d.task_completed,
        "situation_len": len(d.situation),
        "args_hash": args_hash,
    }
