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
import json
import sys
import time
import uuid

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

# Ядро (срез, схема, SGR-блок) — из боевого модуля: проба гоняет ТОТ ЖЕ код, что рантайм.
from sreda.runtime.react_sgr import (  # noqa: E402
    SGR_SYSTEM_BLOCK,
    WIRE_SHAPE_BY_PROVIDER,
    build_wire_schema,
    compute_sgr_tools,
)


def build_anketa_schema(sgr_tools: list, shape: str = "flat") -> dict:
    return build_wire_schema(sgr_tools, shape)


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


# ─────────────────────────── валидация ответа — БОЕВОЙ парсер (CR R1 sol MINOR) ───────────────────────────

def validate_reply(raw: str, tool_calls, sgr_tools: list):
    """Гейт судит ТЕМ ЖЕ parse_sgr_reply, что будет в рантайме (анти-дрейф: проба не должна
    засчитывать ответ, который рантайм отклонит). Возврат (valid, why, SgrDecision|None)."""
    from sreda.runtime.react_sgr import SgrInvalidReply, parse_sgr_reply
    try:
        return True, "", parse_sgr_reply(raw, tool_calls, sgr_tools)
    except SgrInvalidReply as e:
        return False, str(e), None


def matches_expected(decision, expected: tuple) -> bool:
    if expected[0] == "act":
        return decision.kind == "act" and decision.action in expected[1]
    return decision.kind == expected[0]


# ─────────────────────────── прогон ───────────────────────────

def _slice(tools: list) -> list:
    from sreda.runtime.react_loop import _TOOL_NAME_ALIASES
    return compute_sgr_tools(tools, _TOOL_NAME_ALIASES)


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


def run(provider: str, mode: str, timeout_s: float) -> int:
    from sreda.services.llm import get_chat_llm, invoke_with_per_call_timeout

    all_tools = _build_tools_no_db()
    sgr_tools = _slice(all_tools)
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
        valid, why, decision = validate_reply(raw, getattr(resp, "tool_calls", None), sgr_tools)
        ok_sem = bool(valid and decision and matches_expected(decision, expected))
        got = None
        if decision is not None:
            got = decision.kind if decision.kind != "act" else decision.action
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
        st = _slice(_build_tools_no_db())
        schema = build_anketa_schema(st)
        print(json.dumps(schema, ensure_ascii=False, indent=1))
        print(f"-- веток инструментов: {len(st)}: {[t.name for t in st]}", file=sys.stderr)
        return 0
    return run(args.provider, args.mode, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
