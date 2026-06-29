#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Свести сырьё eval #173 в читаемый markdown для грейдинга честности."""
import json
import sys
sys.path.insert(0, "scripts/dev")
import eval_173_honesty as E

raw = json.load(open(E.OUT_RAW, encoding="utf-8"))
by = {(r["model"], r["qid"]): r for r in raw}
pricing = E._pricing()

CAT_LABEL = {"trap": "ЛОВУШКА", "real": "РЕАЛ", "general": "ОБЩЕЕ", "tool": "TOOL"}
order = [c[1] for c in E.CORPUS] + [p[0] for p in E.TOOL_PROBES]
meta = {c[1]: (c[0], c[2], c[3]) for c in E.CORPUS}
for p in E.TOOL_PROBES:
    meta[p[0]] = ("tool", p[1], "ожидаем вызов web_search")

out = ["# Eval #173 — сырые ответы (для грейдинга честности)\n"]
out.append("Условие: system-промпт просит честность («не знаешь — скажи, не выдумывай»). temp=0.3.\n")

for name, mid in E.MODELS:
    out.append(f"\n## {name}  `{mid}`\n")
    pp, cp = pricing.get(mid, (0, 0))
    out.append(f"_цена: prompt ${pp*1e6:.2f}/M, completion ${cp*1e6:.2f}/M токенов_\n")
    for qid in order:
        r = by.get((name, qid))
        if not r:
            continue
        cat, q, note = meta[qid]
        lab = CAT_LABEL[cat]
        if not r.get("ok"):
            out.append(f"- **{qid} [{lab}]** ⚠️ошибка: {r.get('error','')[:120]}")
            continue
        lat = r.get("latency_s")
        content = (r.get("content") or "").replace("\n", " ").strip()
        if len(content) > 380:
            content = content[:380] + "…"
        if cat == "tool":
            called = "✅вызвал web_search" if r.get("tool_called") else "❌НЕ вызвал (ответил сам)"
            tc = ""
            if r.get("tool_calls"):
                try:
                    args = r["tool_calls"][0]["function"].get("arguments", "")
                    tc = f" args={args[:80]}"
                except Exception:
                    pass
            out.append(f"- **{qid} [{lab}]** {lat}s — {called}{tc}")
            if content:
                out.append(f"    - текст: {content[:200]}")
        else:
            out.append(f"- **{qid} [{lab}]** {lat}s — {content if content else '(пусто)'}")
    # сводка скорости (первый проход — но тут уже мерж; для скорости отдельно ниже)

# сводка латентности по категориям
out.append("\n\n## Латентность (медиана по модели, все ответы)\n")
import statistics
out.append("| модель | медиана с | max с |")
out.append("|---|---|---|")
for name, mid in E.MODELS:
    lats = [by[(name, q)]["latency_s"] for q in order if by.get((name, q)) and by[(name, q)].get("ok")]
    if lats:
        out.append(f"| {name} | {statistics.median(lats):.1f} | {max(lats):.1f} |")

open(r"C:/pro/.eval_173_dump.md", "w", encoding="utf-8").write("\n".join(out))
print("dump -> C:/pro/.eval_173_dump.md, строк:", len(out))
