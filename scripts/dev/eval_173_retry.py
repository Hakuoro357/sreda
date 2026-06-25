#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Добить упавшие пары eval #173 (транзиентные обрывы RU-сети) — последовательно, с ретраями."""
import json
import time
import sys
sys.path.insert(0, "scripts/dev")
import eval_173_honesty as E

raw = json.load(open(E.OUT_RAW, encoding="utf-8"))
by_key = {(r["model"], r["qid"]): r for r in raw}

corpus_by_id = {c[1]: c for c in E.CORPUS}
probe_by_id = {p[0]: p for p in E.TOOL_PROBES}
model_id = {name: mid for name, mid in E.MODELS}

failed = [(r["model"], r["qid"]) for r in raw if not r.get("ok")]
print(f"Упало: {len(failed)} -> {failed}")

for name, qid in failed:
    mid = model_id[name]
    for attempt in range(1, 4):
        if qid in corpus_by_id:
            r = E.run_one(name, mid, corpus_by_id[qid])
        else:
            r = E.run_tool(name, mid, probe_by_id[qid])
        status = "OK " if r.get("ok") else "ERR"
        print(f"  {name:24} {qid:6} попытка {attempt}: {status} {r.get('latency_s')}s"
              + ("" if r.get("ok") else f"  {r.get('error','')[:70]}"))
        if r.get("ok"):
            by_key[(name, qid)] = r
            break
        time.sleep(2)
    else:
        by_key[(name, qid)] = r  # сохранить последнюю ошибку

merged = list(by_key.values())
json.dump(merged, open(E.OUT_RAW, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
still = [k for k, v in by_key.items() if not v.get("ok")]
print(f"\nГотово. Осталось упавших: {len(still)} {still}")
