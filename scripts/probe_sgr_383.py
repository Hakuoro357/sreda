"""#383 Ф0 — стоп-гейт: проба полной SGR wire-схемы домена чеклистов (план plans/383-sgr-final.md §7 Ф0).

Проверяет на ЖИВЫХ провайдерах (Mercury-2 / Оса-Groq), что строгий response_format json_schema
держит ПОЛНОЕ объединение sgr_tools (~14 веток с прод-описаниями из реального build_slice_tools)
+ анкету §3, включая МУЛЬТИПРОХОДНЫЕ истории (AIMessage.tool_calls + ToolMessage).

Боевой плумбинг (R6 MINOR#1): вызовы идут через get_chat_llm(provider).bind(response_format=...)
+ invoke_with_per_call_timeout — НЕ прямым HTTP-клиентом.

Гейт (симметрично каждому провайдеру): >=9/10 валидных одношаговых И 0 схемных отказов API
И 3/3 clarify-ловушки И 3/3 валидных мультипроходных. Выход: structured_history_mode (plain|tools_none).

Запуск (env: SREDA_INCEPTION_API_KEY_FILE / SREDA_GROQ_API_KEY_FILE; для Groq — прокси egress):
    python scripts/probe_sgr_383.py --provider inception-mercury2 --mode plain
    python scripts/probe_sgr_383.py --provider groq-gpt-oss-120b --mode plain
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
import uuid

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

SGR_DOMAINS = frozenset({"checklists", "web"})
SGR_EXCLUDED_META = frozenset({"ask_human", "need_family", "delete_my_account"})


# ─────────────────────────── sgr_tools: доменный срез реального набора ───────────────────────────

def compute_sgr_tools(tools: list) -> list:
    """Срез §3 плана: канонизация имени → read∪write домены ⊆ {checklists, web}; мета вне;
    неизвестное имя — fail-closed (исключается)."""
    from sreda.runtime.react_loop import _TOOL_NAME_ALIASES
    from sreda.services.tool_schemas.families import (
        TOOL_OP_CLASS, tool_read_domains, tool_write_domains,
    )
    out = []
    for t in tools:
        if t.name in SGR_EXCLUDED_META:
            continue
        canon = _TOOL_NAME_ALIASES.get(t.name, t.name)
        if canon not in TOOL_OP_CLASS:
            continue  # fail-closed: метаданные не знают инструмент
        doms = set(tool_read_domains(canon)) | set(tool_write_domains(canon))
        if doms and doms <= SGR_DOMAINS:
            out.append(t)
    return out


# ─────────────────────────── strict-нормализатор JSON-схемы аргументов ───────────────────────────

def _inline_defs(node, defs):
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"]
            name = ref.split("/")[-1]
            target = copy.deepcopy(defs.get(name, {}))
            merged = {k: v for k, v in node.items() if k != "$ref"}
            target.update(merged)
            return _inline_defs(target, defs)
        return {k: _inline_defs(v, defs) for k, v in node.items() if k not in ("$defs", "definitions")}
    if isinstance(node, list):
        return [_inline_defs(x, defs) for x in node]
    return node


def strict_normalize(schema: dict) -> dict:
    """Рекурсивно: инлайн $defs; additionalProperties:false; ВСЕ properties → required,
    optional (не были в required) → nullable (anyOf [тип, null])."""
    defs = {**schema.get("$defs", {}), **schema.get("definitions", {})}
    node = _inline_defs(copy.deepcopy(schema), defs)

    def walk(n):
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
                if pname not in prev_req:
                    if not (isinstance(pschema, dict) and isinstance(pschema.get("anyOf"), list)
                            and any(b.get("type") == "null" for b in pschema["anyOf"]
                                    if isinstance(b, dict))):
                        props[pname] = {"anyOf": [pschema, {"type": "null"}]}
            if props:
                n["required"] = sorted(props.keys())
            else:
                # Groq-валидатор: 'required' при пустых properties -> 400
                # ("'required' present but 'properties' is missing"); пустой
                # объект оставляем без required (семантически идентично).
                n.pop("required", None)
        return n

    return walk(node)


def build_tool_branches(sgr_tools: list) -> list[dict]:
    from langchain_core.utils.function_calling import convert_to_openai_tool
    branches = []
    for t in sgr_tools:
        fn = convert_to_openai_tool(t)["function"]
        params = strict_normalize(fn.get("parameters") or {"type": "object", "properties": {}})
        if params.get("type") != "object":
            params = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        branches.append({
            "type": "object", "additionalProperties": False,
            "description": (fn.get("description") or "")[:600],
            "required": ["action", "args"],
            "properties": {"action": {"const": fn["name"]}, "args": params},
        })
    return branches


def build_anketa_schema(sgr_tools: list, shape: str = "flat") -> dict:
    """Wire-схема анкеты — Ф0-поправка к §3 плана: форма ПЕР-ПРОВАЙДЕР (structured_wire_shape).

    - "flat" (Mercury): ПЛОСКИЙ top-level anyOf трёх веток шага, situation ВНУТРИ каждой.
      Причина: двухуровневый конверт {situation, step} Mercury в ~15% случаев ронял (отдавал
      голый step; guided-режим, не constrained) — плоская форма повторяет доказанную #382.
    - "envelope" (Оса/Groq): {situation, step:{anyOf...}} — Groq-валидатор ЗАПРЕЩАЕТ anyOf на
      верхнем уровне (400: schema must have type 'object' ... at the top level), а конверт
      держит идеально (constrained decoding, ни одного дропа в прогоне).

    Семантика идентична; парсер нормализует обе формы в плоскую. Схемная связка
    enough_data⇔kind сохранена per-branch в обеих формах."""
    if shape == "envelope":
        flat = build_anketa_schema(sgr_tools, "flat")
        step_branches = []
        for br in flat["anyOf"]:
            b = copy.deepcopy(br)
            b["properties"].pop("situation", None)
            b["required"] = [k for k in b["required"] if k != "situation"]
            step_branches.append(b)
        return {
            "type": "object", "additionalProperties": False,
            "required": ["situation", "step"],
            "properties": {
                "situation": {"type": "string", "maxLength": 400},
                "step": {"anyOf": step_branches},
            },
        }
    return {"anyOf": [
        {"type": "object", "additionalProperties": False,
         "required": ["kind", "situation", "enough_data", "tool"],
         "properties": {
             "kind": {"const": "act"},
             "situation": {"type": "string", "maxLength": 400},
             "enough_data": {"const": True},
             "tool": {"anyOf": build_tool_branches(sgr_tools)},
         }},
        {"type": "object", "additionalProperties": False,
         "required": ["kind", "situation", "enough_data", "question"],
         "properties": {
             "kind": {"const": "clarify"},
             "situation": {"type": "string", "maxLength": 400},
             "enough_data": {"const": False},
             "question": {"type": "string", "maxLength": 300},
         }},
        {"type": "object", "additionalProperties": False,
         "required": ["kind", "situation", "task_completed", "reply"],
         "properties": {
             "kind": {"const": "finish"},
             "situation": {"type": "string", "maxLength": 400},
             "task_completed": {"const": True},
             "reply": {"type": "string"},
         }},
    ]}


SGR_SYSTEM_BLOCK = (
    "Ты — Среда, семейная помощница. Сейчас ты работаешь с разделом «Списки дел» (чек-листы).\n"
    "Отвечай ТОЛЬКО объектом по заданной схеме — анкетой шага:\n"
    "- situation: 1-2 фразы — что просит пользователь и что уже сделано в этом ходе.\n"
    "- step: РОВНО ОДНО из трёх:\n"
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


# ─────────────────────────── набор Ф0 (заморожен ДО прогона) ───────────────────────────

SINGLE_SHOT = [
    # (id, фраза, ожидание) — ожидание: ("act", {допустимые действия}) | ("clarify",) | ("finish",)
    ("s1", "Покажи список покупок на дачу", ("act", {"show_checklist", "get_checklist", "list_checklist_items"})),
    ("s2", "Какие у меня есть списки дел?", ("act", {"list_checklists", "get_checklist"})),
    ("s3", "Добавь молоко и хлеб в список продуктов", ("act", {"add_checklist_items"})),
    ("s4", "Отметь пункт «хлеб» в списке продуктов как сделанный", ("act", {"mark_checklist_item_done"})),
    ("s5", "Создай список «Ремонт» с пунктами шпаклёвка и краска", ("act", {"create_checklist", "add_checklist_items"})),
    ("t1", "Добавь в список", ("clarify",)),
    ("t2", "Удали пункт из списка", ("clarify",)),
    ("t3", "Отметь как сделанное", ("clarify",)),
    ("f1", "Спасибо, больше ничего не нужно", ("finish",)),
    ("f2", "Что ты умеешь делать со списками дел?", ("finish",)),
]


def _mk_tc(name: str, args: dict) -> dict:
    return {"name": name, "args": args, "id": f"sgr-{uuid.uuid4().hex[:8]}", "type": "tool_call"}


def multiturn_trajectories() -> list[tuple[str, list, tuple]]:
    """3 траектории §7 Ф0: история с AIMessage.tool_calls + ToolMessage."""
    tc1 = _mk_tc("show_checklist", {"name": "продукты"})
    m1 = [
        HumanMessage(content="Покажи список продуктов"),
        AIMessage(content="", tool_calls=[tc1]),
        ToolMessage(content="Список «Продукты»: 1) молоко — не куплено; 2) хлеб — куплен.",
                    tool_call_id=tc1["id"], name="show_checklist"),
    ]
    tc2 = _mk_tc("add_checklist_items", {"name": "продукты", "items": ["молоко"]})
    m2 = [
        HumanMessage(content="Добавь молоко в продукты и покажи, что получилось"),
        AIMessage(content="", tool_calls=[tc2]),
        ToolMessage(content="Добавлено в «Продукты»: молоко.",
                    tool_call_id=tc2["id"], name="add_checklist_items"),
    ]
    tc3 = _mk_tc("ask_human", {"question": "В какой список и что добавить?"})
    m3 = [
        HumanMessage(content="Добавь в список"),
        AIMessage(content="", tool_calls=[tc3]),
        ToolMessage(content="в продукты — молоко", tool_call_id=tc3["id"], name="ask_human"),
    ]
    return [
        ("m1_act_tool_finish", m1, ("finish",)),
        ("m2_act_tool_act", m2, ("act", {"show_checklist", "get_checklist", "list_checklist_items"})),
        ("m3_clarify_resume_act", m3, ("act", {"add_checklist_items"})),
    ]


# ─────────────────────────── валидация ответа ───────────────────────────

def validate_reply(raw: str, sgr_tools: list) -> tuple[bool, str, dict | None]:
    """Минимальная строгая проверка формы (Ф0; в Ф1 — pydantic). Возврат (valid, why, parsed)."""
    try:
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        return False, f"json_error:{type(e).__name__}", None
    if not isinstance(data, dict):
        return False, "top_type", None
    # Нормализация конверт → плоская (Ф0: у Осы форма envelope)
    if set(data.keys()) == {"situation", "step"} and isinstance(data.get("step"), dict):
        data = {"situation": data["situation"], **data["step"]}
    step = data  # дальше — единая плоская форма
    if not isinstance(step.get("situation"), str) or not (0 < len(step["situation"]) <= 400):
        return False, "situation", data
    kind = step.get("kind")
    allowed_keys = {
        "act": {"kind", "situation", "enough_data", "tool"},
        "clarify": {"kind", "situation", "enough_data", "question"},
        "finish": {"kind", "situation", "task_completed", "reply"},
    }
    if kind in allowed_keys and set(step.keys()) != allowed_keys[kind]:
        return False, "branch_keys", data
    names = {t.name for t in sgr_tools}
    if kind == "act":
        if step.get("enough_data") is not True:
            return False, "act_enough_data", data
        tool = step.get("tool")
        if not isinstance(tool, dict) or set(tool.keys()) != {"action", "args"}:
            return False, "act_tool_shape", data
        if tool.get("action") not in names:
            return False, f"act_unknown_tool:{tool.get('action')}", data
        if not isinstance(tool.get("args"), dict):
            return False, "act_args_type", data
        # null-вычистка (§3) + прогон через args_schema инструмента
        args = {k: v for k, v in tool["args"].items() if v is not None}
        t = next(t for t in sgr_tools if t.name == tool["action"])
        try:
            t.args_schema.model_validate(args)
        except Exception as e:  # noqa: BLE001
            return False, f"act_args_invalid:{type(e).__name__}", data
        return True, "", data
    if kind == "clarify":
        if step.get("enough_data") is not False:
            return False, "clarify_enough_data", data
        q = step.get("question")
        if not isinstance(q, str) or not (0 < len(q) <= 300):
            return False, "clarify_question", data
        return True, "", data
    if kind == "finish":
        if step.get("task_completed") is not True:
            return False, "finish_task_completed", data
        if not isinstance(step.get("reply"), str) or not step["reply"]:
            return False, "finish_reply", data
        return True, "", data
    return False, f"kind:{kind}", data


def matches_expected(parsed: dict, expected: tuple) -> bool:
    step = parsed  # плоская форма
    if expected[0] == "act":
        return step.get("kind") == "act" and step.get("tool", {}).get("action") in expected[1]
    return step.get("kind") == expected[0]


# ─────────────────────────── прогон ───────────────────────────

def _build_tools_no_db() -> list:
    """build_slice_tools(session=None) для схемной части: сборка зовёт EntitlementGate.check
    (SQL) — подменяем на щедрый grandfathered-результат (нужен только тир web_search-капа;
    на состав sgr-среза не влияет, инструменты НЕ исполняются)."""
    from sreda.services import entitlement_gate as _eg
    _orig = _eg.EntitlementGate.check
    _eg.EntitlementGate.check = (  # type: ignore[method-assign]
        lambda self, tenant_id: _eg.GateResult(
            allowed=True, reason="ok", plan_key="probe", is_grandfathered=True))
    try:
        from sreda.runtime.react_loop import build_slice_tools
        return build_slice_tools(None, "probe383-tenant", "probe383-user")
    finally:
        _eg.EntitlementGate.check = _orig  # type: ignore[method-assign]


WIRE_SHAPE_BY_PROVIDER = {"inception-mercury2": "flat", "groq-gpt-oss-120b": "envelope"}


def run(provider: str, mode: str, timeout_s: float) -> int:
    from sreda.services.llm import get_chat_llm, invoke_with_per_call_timeout

    all_tools = _build_tools_no_db()
    sgr_tools = compute_sgr_tools(all_tools)
    shape = WIRE_SHAPE_BY_PROVIDER[provider]
    schema = build_anketa_schema(sgr_tools, shape)
    branch_names = [t.name for t in sgr_tools]
    print(f"provider={provider} mode={mode} shape={shape}")
    print(f"sgr_tools ({len(branch_names)}): {branch_names}")
    print(f"wire schema bytes: {len(json.dumps(schema, ensure_ascii=False))}")
    assert "$ref" not in json.dumps(schema), "инвариант §3: без $ref"

    llm = get_chat_llm(provider=provider)
    bind_kwargs: dict = {"response_format": {
        "type": "json_schema",
        "json_schema": {"name": "sgr_step", "schema": schema, "strict": True},
    }}
    if mode == "tools_none":
        from langchain_core.utils.function_calling import convert_to_openai_tool
        bind_kwargs["tools"] = [convert_to_openai_tool(t) for t in sgr_tools]
        bind_kwargs["tool_choice"] = "none"
    bound = llm.bind(**bind_kwargs)

    results = []
    api_schema_failures = 0

    def one_call(case_id: str, msgs: list, expected: tuple):
        nonlocal api_schema_failures
        t0 = time.perf_counter()
        try:
            resp = invoke_with_per_call_timeout(bound, msgs, timeout_seconds=timeout_s,
                                                provider=provider)
        except Exception as e:  # noqa: BLE001
            ms = int((time.perf_counter() - t0) * 1000)
            api_schema_failures += 1
            results.append({"id": case_id, "valid": False, "why": f"api:{type(e).__name__}",
                            "expected": expected[0], "got": None, "ms": ms})
            print(f"  {case_id}: API-FAIL {type(e).__name__} ({ms}ms)")
            return
        ms = int((time.perf_counter() - t0) * 1000)
        raw = resp.content if isinstance(resp.content, str) else str(resp.content)
        if getattr(resp, "tool_calls", None):
            results.append({"id": case_id, "valid": False, "why": "native_tool_calls",
                            "expected": expected[0], "got": "native_tool_calls", "ms": ms})
            print(f"  {case_id}: INVALID native_tool_calls ({ms}ms)")
            return
        valid, why, parsed = validate_reply(raw, sgr_tools)
        ok_sem = bool(valid and parsed and matches_expected(parsed, expected))
        got = None
        if isinstance(parsed, dict):
            got = (parsed.get("kind") if parsed.get("kind") != "act"
                   else (parsed.get("tool") or {}).get("action"))
        exp_ser = [expected[0], sorted(expected[1])] if len(expected) > 1 else [expected[0]]
        results.append({"id": case_id, "valid": valid, "why": why, "expected": exp_ser,
                        "got": got, "match": ok_sem, "ms": ms,
                        "raw": raw[:1500]})
        mark = "OK " if (valid and ok_sem) else ("VAL" if valid else "BAD")
        print(f"  {case_id}: {mark} got={got} expected={expected[0]}"
              f"{('/' + '|'.join(sorted(expected[1]))) if len(expected) > 1 else ''}"
              f" ({ms}ms){'' if valid else ' why=' + why}")

    print("— одношаговые —")
    for cid, phrase, expected in SINGLE_SHOT:
        one_call(cid, [SystemMessage(content=SGR_SYSTEM_BLOCK), HumanMessage(content=phrase)],
                 expected)

    print("— мультипроходные —")
    for cid, msgs, expected in multiturn_trajectories():
        one_call(cid, [SystemMessage(content=SGR_SYSTEM_BLOCK), *msgs], expected)

    singles = [r for r in results if r["id"][0] in "stf"]
    traps = [r for r in results if r["id"].startswith("t")]
    multi = [r for r in results if r["id"].startswith("m")]
    n_valid = sum(1 for r in singles if r["valid"])
    n_trap = sum(1 for r in traps if r["valid"] and r.get("match"))
    n_multi = sum(1 for r in multi if r["valid"])
    n_multi_sem = sum(1 for r in multi if r["valid"] and r.get("match"))
    n_match = sum(1 for r in singles if r.get("match"))
    lat = sorted(r["ms"] for r in results)
    med = lat[len(lat) // 2] if lat else -1

    print("— ИТОГ —")
    print(f"валидных одношаговых: {n_valid}/10 (гейт >=9); семантика верна: {n_match}/10")
    print(f"схемных отказов API: {api_schema_failures} (гейт 0)")
    print(f"ловушки → clarify: {n_trap}/3 (гейт 3/3)")
    print(f"мультипроходных валидных: {n_multi}/3 (гейт 3/3); семантика: {n_multi_sem}/3")
    print(f"медиана латентности: {med} мс")
    gate = (n_valid >= 9 and api_schema_failures == 0 and n_trap == 3 and n_multi == 3)
    print(f"ГЕЙТ {provider} mode={mode}: {'PASS' if gate else 'FAIL'}")

    out = {"provider": provider, "mode": mode, "shape": shape, "gate": gate,
           "singles_valid": n_valid, "singles_match": n_match,
           "api_schema_failures": api_schema_failures,
           "traps_clarify": n_trap, "multi_valid": n_multi, "multi_match": n_multi_sem,
           "median_ms": med, "sgr_tools": branch_names, "results": results}
    fname = f"probe383_{provider.replace('/', '_')}_{mode}.json"
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"результаты: {fname}")
    return 0 if gate else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True,
                    choices=["inception-mercury2", "groq-gpt-oss-120b"])
    ap.add_argument("--mode", default="plain", choices=["plain", "tools_none"])
    ap.add_argument("--timeout", type=float, default=45.0)
    ap.add_argument("--schema-only", action="store_true",
                    help="только собрать и напечатать схему (без вызовов)")
    args = ap.parse_args()
    if args.schema_only:
        st = compute_sgr_tools(_build_tools_no_db())
        schema = build_anketa_schema(st)
        print(json.dumps(schema, ensure_ascii=False, indent=1))
        print(f"-- веток инструментов: {len(st)}: {[t.name for t in st]}", file=sys.stderr)
        return 0
    return run(args.provider, args.mode, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
