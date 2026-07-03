"""#285 Фаза A (срез A3): shadow-отчёт — TurnPolicy vs фактические исполнения.

Гоняется НА ПРОДЕ после деплоя shadow (флаг ON, список пуст). Выход из Фазы A (план +
285-phase0-decisions.md п.3): РАСХОЖДЕНИЙ НЕТ и каунты классов событий набраны
(confirm/need_family/resume/guard-признаки) — либо синтетические пробы.

Сверка НЕ тавтологична (в отличие от отвергнутого рантайм-варианта, 285-phaseA-design.md):
полиси строится на СТАРТЕ хода из intent-решения, а исполнения включают всю динамику
(need_family-догрузки, guard-recovery, fallback) — расхождение ловит именно те двери,
которые Фаза B обязана гейтить.

Правила сверки (shadow выражает сплит):
- web_scope_only=True (chat/fact): исполненные инструменты ⊆ WEB_ONLY + мета — иначе mismatch
  (сегодняшний сплит это гарантирует кодом; расхождение = баг сайдкара ИЛИ дыра сплита).
- task/OFF: allowed_write_domains не-None → исполненный write вне списка = mismatch
  (guard A4 / фильтры это держат); None → без проверки (фильтра не было).
Только агрегаты, без ПД (гарантия — тест test_285_shadow_report_output_guard).
"""

from __future__ import annotations

import json
from collections import Counter

from sqlalchemy import select

from sreda.db.models.react_trace import ReactTurnTrace
from sreda.db.session import get_session_factory
from sreda.services.tool_schemas.families import TOOL_OP_CLASS, tool_write_domains

_META = {"ask_human", "need_family", "delete_my_account"}
_WEB_ONLY = {"web_search", "fetch_url", "get_weather"}


def compute(rows) -> dict:
    agg = {
        "total_done": 0, "with_policy": 0, "policy_null": 0,
        "variant": Counter(), "mismatch_web_scope": 0, "mismatch_write_domain": 0,
        "mismatch_tools": Counter(),
        "events": Counter(),  # классы событий для выхода из фазы
        "window": [None, None],
    }
    for r in rows:
        agg["total_done"] += 1
        ca = getattr(r, "created_at", None)
        if ca is not None:
            agg["window"][0] = min(agg["window"][0] or ca, ca)
            agg["window"][1] = max(agg["window"][1] or ca, ca)
        # каунты классов событий (независимо от полиси)
        if (r.confirm_state or "none") != "none":
            agg["events"]["confirm_pause"] += 1
        if r.confirm_resolution:
            agg["events"][f"confirm_{r.confirm_resolution}"] += 1
        try:
            tcs = json.loads(r.tool_calls_json or "[]")
        except (TypeError, ValueError):
            tcs = []
        if any(str(c.get("name")) == "need_family" for c in tcs):
            agg["events"]["need_family"] += 1
        if (r.passes or 0) > 2:
            agg["events"]["multi_pass_gt2"] += 1
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
        executed = [c for c in tcs
                    if c.get("result_kind") == "ok" and c.get("observed", True)]
        if pol.get("web_scope_only"):
            bad = [str(c.get("name")) for c in executed
                   if str(c.get("name")) not in (_WEB_ONLY | _META)]
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
    lines.append(f"done turns: {agg['total_done']}; window: {w0}..{w1}")
    lines.append(f"с полиси (shadow ON): {agg['with_policy']}; без полиси: {agg['policy_null']}")
    lines.append("варианты полиси: " + ", ".join(f"{k}:{v}" for k, v in agg["variant"].most_common()))
    lines.append(f"MISMATCH web-scope (chat/fact исполнил вне web+meta): {agg['mismatch_web_scope']}")
    lines.append(f"MISMATCH write-domain (write вне allowed при не-None): {agg['mismatch_write_domain']}")
    if agg["mismatch_tools"]:
        lines.append("инструменты расхождений: " +
                     ", ".join(f"{k}:{v}" for k, v in agg["mismatch_tools"].most_common(10)))
    lines.append("каунты классов событий (выход Фазы A требует ненулевые или синтетические пробы):")
    for k, v in sorted(agg["events"].items()):
        lines.append(f"  {k}: {v}")
    verdict = "OK: расхождений нет" if (agg["mismatch_web_scope"] + agg["mismatch_write_domain"]) == 0 \
        else "РАСХОЖДЕНИЯ ЕСТЬ — разбирать до Фазы B"
    lines.append(verdict)
    return "\n".join(lines)


def main() -> None:
    with get_session_factory()() as s:
        rows = s.execute(
            select(ReactTurnTrace).where(ReactTurnTrace.status == "done")
        ).scalars().all()
    print(render(compute(rows)))


if __name__ == "__main__":
    main()
