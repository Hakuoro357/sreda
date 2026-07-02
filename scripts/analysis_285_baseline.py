"""#285 Фаза 0: базлайн — доля write-ходов без детерминированного сигнала (v2, после R1 фазового ревью).

Гоняется НА ПРОДЕ (нужны env-ключи шифрования + БД): офлайн-переигрывание детекторов по
`react_turn_trace.origin_user_text` + `tool_calls_json`. Выводит ТОЛЬКО агрегаты — никаких
текстов пользователей/аргументов (гарантия закреплена тестом test_285_baseline_output_guard).

Детекторы (РАЗДЕЛЬНЫЕ счётчики — вывод «что двигает лексикон» должен читаться из чисел):
- old   = сегодняшний ИНТЕНТ-сигнал #197/#215: `_must_task` | `_section_hint` (сравнительное поле;
          в write-гейт яруса (а) `_section_hint` НЕ входит — это директивный слой).
- cmd   = old(must_task-часть) + командные глаголы-мутации (корпус §1.1).
- decl  = + декларативные stable-fact паттерны (корпус §1.2).
- v0    = write-сигнал v0 = must_task | cmd | decl (БЕЗ _section_hint).

Единица go/no-go: ДОЛЯ ХОДОВ (per-turn — канон, инвентарь §3); per-execution печатается вторым числом.
Оговорки: confirm-ходы отдельно (#269); rk=="ok" в хранимом трейсе — best-effort (отсутствующий
ToolMessage дефолтится в ok; честное поле — Фаза A).
"""

from __future__ import annotations

import json
import re
from collections import Counter

from sqlalchemy import select

from sreda.db.models.react_trace import ReactTurnTrace
from sreda.db.session import get_session_factory
from sreda.runtime.react_preflight import _must_task, _section_hint
from sreda.services.tool_schemas.families import TOOL_OP_CLASS, tool_read_domains, tool_write_domains

# Командные глаголы-мутации (корпус §1.1). Щедрые корни = замер recall, не боевой гейт.
# Отсечены известные паразиты: «удалось/удалась» (не команда «удали»).
_CMD = re.compile(
    r"\b(добав|сохран|запиш|заплан|внес|отмет|вычеркн|постав|напомн|создай|отмен|перенес"
    r"|удал(?!ось|ась|ились|ился|яться|ённ)(?!о\b))[а-яё]*",
    re.IGNORECASE,
)
# Декларативы (корпус §1.2): скоуп-ограниченная заглавная у ПЕРВОГО слова ([Мм]еня…) — НЕ глобальный
# IGNORECASE (иначе рухнет отсечение near-miss по заглавной букве ИМЕНИ: «меня зовут на дачу»).
_DECL = [
    re.compile(r"\b[Мм]еня зовут\s+[А-ЯЁ][а-яё]+"),
    re.compile(r"\b([Яя]\s+)?[Жж]иву в\s+[А-ЯЁ]"),
    re.compile(r"\b[Уу] меня (двое|трое|четверо|\d+)\s*(дет|реб|сын|доч)"),
    re.compile(r"\b(мужа|жену|сына|дочь|дочку|кота|собаку)\s+зовут\s+[А-ЯЁ]"),
]

_WRITE_KINDS = ("write",)


def _detectors(text: str) -> dict[str, bool]:
    mt = bool(_must_task(text))
    old = mt or (_section_hint(text) is not None)
    cmd = bool(_CMD.search(text))
    decl = any(p.search(text) for p in _DECL)
    return {"old": old, "must_task": mt, "cmd": cmd, "decl": decl, "v0": mt or cmd or decl}


def compute(rows) -> dict:
    """Чистая агрегация (тестируема на фейк-строках). Возвращает ТОЛЬКО счётчики/имена инструментов."""
    agg = {
        "total": 0, "fresh": 0, "confirm_turns": 0, "confirm_w_write": 0,
        "fresh_w_write": 0, "w_exec_total": 0,
        "unsig_turns": {"old": 0, "v0": 0, "v0_wo_cmd": 0, "v0_wo_decl": 0},
        "unsig_w_exec": {"old": 0, "v0": 0},
        "unsig_tools": Counter(), "writes_per_unsig_turn": [],
        "write_domains_per_unsig_turn": [], "read_domains_per_unsig_turn": [],
        "unsig_turns_with_own_read": 0, "own_read_exec_on_unsig": 0,
        "window": [None, None], "tenants": set(),
    }
    for r in rows:
        agg["total"] += 1
        ca = getattr(r, "created_at", None)
        if ca is not None:
            agg["window"][0] = min(agg["window"][0] or ca, ca)
            agg["window"][1] = max(agg["window"][1] or ca, ca)
        agg["tenants"].add(getattr(r, "tenant_id", None))
        try:
            tcs = json.loads(r.tool_calls_json or "[]")
        except (TypeError, ValueError):
            tcs = []
        writes = [c for c in tcs
                  if TOOL_OP_CLASS.get(str(c.get("name"))) in _WRITE_KINDS
                  and c.get("result_kind") == "ok"]
        if (r.confirm_state or "none") != "none":
            agg["confirm_turns"] += 1
            if writes:
                agg["confirm_w_write"] += 1
            continue  # #269: отдельная корзина
        agg["fresh"] += 1
        if not writes:
            continue
        agg["fresh_w_write"] += 1
        agg["w_exec_total"] += len(writes)
        d = _detectors(r.origin_user_text or "")
        if not d["old"]:
            agg["unsig_turns"]["old"] += 1
            agg["unsig_w_exec"]["old"] += len(writes)
        if not d["v0"]:
            agg["unsig_turns"]["v0"] += 1
            agg["unsig_w_exec"]["v0"] += len(writes)
            agg["writes_per_unsig_turn"].append(len(writes))
            for c in writes:
                agg["unsig_tools"][str(c.get("name"))] += 1
            own_reads = [c for c in tcs
                         if TOOL_OP_CLASS.get(str(c.get("name"))) == "read_pure"
                         and c.get("result_kind") == "ok"]  # web отсечён op_class'ом (read_external)
            if own_reads:
                agg["unsig_turns_with_own_read"] += 1
                agg["own_read_exec_on_unsig"] += len(own_reads)
            # доменный fanout хода (R2 MINOR: форма batch-превью и bounded-маппера)
            wd: set[str] = set()
            for c in writes:
                wd |= set(tool_write_domains(str(c.get("name"))))
            agg["write_domains_per_unsig_turn"].append(len(wd))
            rd: set[str] = set()
            for c in own_reads:
                rd |= set(tool_read_domains(str(c.get("name"))))
            agg["read_domains_per_unsig_turn"].append(len(rd))
        # декомпозиция: чем был бы v0 без каждого слоя
        if not (d["must_task"] or d["decl"]):
            agg["unsig_turns"]["v0_wo_cmd"] += 1
        if not (d["must_task"] or d["cmd"]):
            agg["unsig_turns"]["v0_wo_decl"] += 1
    return agg


def _pct(a: int, b: int) -> str:
    return f"{a}/{b}" + (f" ({100 * a / b:.0f}%)" if b else "")


def render(agg: dict) -> str:
    lines = []
    w0, w1 = agg["window"]
    lines.append(f"done turns: {agg['total']}; window: {w0}..{w1}; tenants: {len(agg['tenants'])}")
    lines.append(f"fresh: {agg['fresh']}; confirm turns: {agg['confirm_turns']} "
                 f"(with writes: {agg['confirm_w_write']} — отдельная корзина, #269)")
    fw = agg["fresh_w_write"]
    lines.append(f"fresh turns with executed writes: {fw}; write executions total: {agg['w_exec_total']}")
    lines.append("GO/NO-GO (per-turn, канон): write-ходы БЕЗ сигнала:")
    lines.append(f"  old(#197 intent, сравнительное): {_pct(agg['unsig_turns']['old'], fw)}")
    lines.append(f"  v0 write-сигнал (must_task|cmd|decl): {_pct(agg['unsig_turns']['v0'], fw)}")
    lines.append(f"  декомпозиция: без cmd-слоя было бы {_pct(agg['unsig_turns']['v0_wo_cmd'], fw)}; "
                 f"без decl-слоя {_pct(agg['unsig_turns']['v0_wo_decl'], fw)}")
    lines.append(f"per-execution (второе число): old {_pct(agg['unsig_w_exec']['old'], agg['w_exec_total'])}; "
                 f"v0 {_pct(agg['unsig_w_exec']['v0'], agg['w_exec_total'])}")
    wpt = sorted(agg["writes_per_unsig_turn"])
    if wpt:
        n = len(wpt)
        p = lambda q: wpt[min(n - 1, int(q * n))]
        lines.append(f"writes/ход на v0-безсигнальных: p50={p(0.5)} p90={p(0.9)} p95={p(0.95)} max={wpt[-1]} (n={n})")
    lines.append(f"v0-безсигнальные ходы с ≥1 own-data read: {agg['unsig_turns_with_own_read']} "
                 f"(исполнений reads: {agg['own_read_exec_on_unsig']})")
    for key, label in (("write_domains_per_unsig_turn", "write-доменов/ход"),
                       ("read_domains_per_unsig_turn", "read-доменов/ход")):
        vals = agg[key]
        if vals:
            dist = Counter(vals)
            lines.append(f"{label} на v0-безсигнальных: " +
                         ", ".join(f"{k}дом:{dist[k]}" for k in sorted(dist)) + f"; max={max(vals)}")
    lines.append("top write-инструменты на v0-безсигнальных ходах (ИСПОЛНЕНИЙ, не ходов):")
    for name, cnt in agg["unsig_tools"].most_common(10):
        lines.append(f"  {name}: {cnt}")
    return "\n".join(lines)


def main() -> None:
    with get_session_factory()() as s:
        rows = s.execute(
            select(ReactTurnTrace).where(ReactTurnTrace.status == "done")
        ).scalars().all()
    print(render(compute(rows)))


if __name__ == "__main__":
    main()
