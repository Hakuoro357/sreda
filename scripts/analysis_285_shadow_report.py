"""#285 Фаза A (срез A3): shadow-отчёт v2 — TurnPolicy vs фактические исполнения.

Гоняется НА ПРОДЕ после деплоя shadow (флаг ON, список пуст):
    python analysis_285_shadow_report.py --since 2026-07-04
Выход из Фазы A (285-phase0-decisions.md п.3): расхождений нет И каунты классов событий
(guard/need_family/resume/confirm) набраны — либо синтетические пробы.

v2 (R1 фазового ревью Фазы A):
- ОКНО обязательно осмысленно: --since (ISO-дата) отрезает до-shadow историю; with_policy==0 → FAIL.
- web-scope СТРОГО против WEB_ONLY_TOOL_NAMES (react_policy, единый источник) — БЕЗ извинения
  меты: сплит на chat/fact мету не биндит вовсе, её ok-исполнение = дыра, не шум (CodexM+субагент).
- guard-каунты из turn_events полиси (guard_attempted/guard_full/resumed/passes) + Counter по
  result_kind всех вызовов (unavailable/domain_blocked/search_limit/error).
- confirm-события по confirm_resolution (yes|no|redirect), не по confirm_state (тот конфлейтит
  ask_human-resume).
- Orphan-записи (collect_tool_calls шаг 3) делают confirm-ходы аудируемыми; их счёт печатается.
Сверка нетавтологична: полиси со старта хода, исполнения — вся динамика. Только агрегаты, без ПД.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import select

from sreda.db.models.react_trace import ReactTurnTrace
from sreda.db.session import get_session_factory
from sreda.runtime.react_policy import WEB_ONLY_TOOL_NAMES
from sreda.services.tool_schemas.families import TOOL_OP_CLASS, tool_write_domains

_WEB_ONLY = frozenset(WEB_ONLY_TOOL_NAMES)


def compute(rows) -> dict:
    agg = {
        "total_done": 0, "with_policy": 0, "policy_null": 0,
        "variant": Counter(), "mismatch_web_scope": 0, "mismatch_write_domain": 0,
        "mismatch_tools": Counter(),
        "events": Counter(), "result_kinds": Counter(),
        "confirm_rows": 0, "orphan_records": 0,
        "window": [None, None],
    }
    for r in rows:
        agg["total_done"] += 1
        ca = getattr(r, "created_at", None)
        if ca is not None:
            agg["window"][0] = min(agg["window"][0] or ca, ca)
            agg["window"][1] = max(agg["window"][1] or ca, ca)
        try:
            tcs = json.loads(r.tool_calls_json or "[]")
        except (TypeError, ValueError):
            tcs = []
        # классы событий (выход фазы) — confirm по resolution, не по confirm_state (m7)
        if r.confirm_resolution:
            agg["events"][f"confirm_{r.confirm_resolution}"] += 1
        if (r.confirm_state or "none") != "none":
            agg["confirm_rows"] += 1
        if any(str(c.get("name")) == "need_family" for c in tcs):
            agg["events"]["need_family"] += 1
        for c in tcs:
            agg["result_kinds"][str(c.get("result_kind"))] += 1
            if c.get("orphan"):
                agg["orphan_records"] += 1
        if r.outcome and r.outcome != "ok":
            agg["events"][f"outcome_{r.outcome}"] += 1

        tpj = getattr(r, "turn_policy_json", None)
        if not tpj:
            agg["policy_null"] += 1
            continue
        try:
            pol = json.loads(tpj)
        except (TypeError, ValueError):
            agg["policy_null"] += 1
            continue
        agg["with_policy"] += 1
        agg["variant"][pol.get("prompt_variant") or "?"] += 1
        ev = pol.get("turn_events") or {}
        if ev.get("resumed"):
            agg["events"]["resumed"] += 1
        if ev.get("guard_attempted"):
            agg["events"]["guard_attempted"] += 1
        if ev.get("guard_full"):
            agg["events"]["guard_full"] += 1

        executed = [c for c in tcs
                    if c.get("result_kind") == "ok" and c.get("observed", True)]
        if pol.get("web_scope_only"):
            # СТРОГО web-only: мета НЕ извиняется — сплит её на chat/fact не биндит,
            # ok-исполнение меты здесь = дыра сплита/сайдкара (R1 MAJOR CodexM+субагент).
            bad = [str(c.get("name")) for c in executed if str(c.get("name")) not in _WEB_ONLY]
            if bad:
                agg["mismatch_web_scope"] += 1
                for n in bad:
                    agg["mismatch_tools"][n] += 1
        else:
            aw = pol.get("allowed_write_domains")
            if aw is not None:
                allowed = set(aw)
                bad = [str(c.get("name")) for c in executed
                       if TOOL_OP_CLASS.get(str(c.get("name"))) == "write"
                       and not (set(tool_write_domains(str(c.get("name")))) <= allowed)]
                if bad:
                    agg["mismatch_write_domain"] += 1
                    for n in bad:
                        agg["mismatch_tools"][n] += 1
    return agg


def render(agg: dict) -> str:
    lines = []
    w0, w1 = agg["window"]
    lines.append(f"done turns (в окне): {agg['total_done']}; факт. окно: {w0}..{w1}")
    lines.append(f"с полиси (shadow ON): {agg['with_policy']}; без полиси: {agg['policy_null']}")
    lines.append("варианты полиси: " + (", ".join(f"{k}:{v}" for k, v in agg["variant"].most_common()) or "-"))
    lines.append(f"MISMATCH web-scope (chat/fact исполнил вне web-тройки, вкл. мету): {agg['mismatch_web_scope']}")
    lines.append(f"MISMATCH write-domain (write вне allowed при не-None): {agg['mismatch_write_domain']}")
    if agg["mismatch_tools"]:
        lines.append("инструменты расхождений: " +
                     ", ".join(f"{k}:{v}" for k, v in agg["mismatch_tools"].most_common(10)))
    lines.append(f"confirm-строк: {agg['confirm_rows']} (аудируемы через orphan-записи: "
                 f"{agg['orphan_records']} записей)")
    lines.append("классы событий (выход фазы: guard/need_family/resume/confirm ненулевые или синтетические пробы):")
    for k, v in sorted(agg["events"].items()):
        lines.append(f"  {k}: {v}")
    lines.append("result_kind всех вызовов: " +
                 (", ".join(f"{k}:{v}" for k, v in agg["result_kinds"].most_common()) or "-"))
    if agg["with_policy"] == 0:
        lines.append("FAIL: ни одной строки с полиси в окне — shadow не работает либо окно неверно")
    elif (agg["mismatch_web_scope"] + agg["mismatch_write_domain"]) == 0:
        lines.append("OK: расхождений нет")
    else:
        lines.append("РАСХОЖДЕНИЯ ЕСТЬ — разбирать до Фазы B")
    return "\n".join(lines)


def exit_status(agg: dict) -> int:
    """Выходной код гейта (R2 CodexH MAJOR): 0 ТОЛЬКО когда shadow реально сработал (with_policy>0)
    И расхождений нет; иначе 1 — чтобы автоматизация/CI не пропустили FAIL при exit 0."""
    if agg["with_policy"] == 0:
        return 1
    if (agg["mismatch_web_scope"] + agg["mismatch_write_domain"]) > 0:
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True,
                    help="ISO-дата начала shadow-окна (например 2026-07-04) — отрезает до-shadow историю")
    args = ap.parse_args()
    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    with get_session_factory()() as s:
        rows = s.execute(
            select(ReactTurnTrace).where(ReactTurnTrace.status == "done",
                                         ReactTurnTrace.created_at >= since)
        ).scalars().all()
    agg = compute(rows)
    print(render(agg))
    return exit_status(agg)


if __name__ == "__main__":
    import sys
    sys.exit(main())
