#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Перепроверить ПУСТЫЕ/garbled ответы think-моделей с большим max_tokens (честность к данным)."""
import json
import time
import urllib.request
import sys
sys.path.insert(0, "scripts/dev")
import eval_173_honesty as E

# (model_name, qid) — пустые/garbled из первого прохода
RECHECK = [
    ("qwen3-next-80b-thinking", "T5"),
    ("qwen3-next-80b-thinking", "R1"),
    ("qwen3-next-80b-thinking", "R2"),
    ("qwen3-next-80b-thinking", "G2"),
    ("trinity-large-thinking", "R3"),
    ("mimo-2.5-pro", "R1"),
]
corpus_by_id = {c[1]: c for c in E.CORPUS}
model_id = {n: m for n, m in E.MODELS}


def call_big(model_id_, q, max_tokens=3000):
    body = {"model": model_id_,
            "messages": [{"role": "system", "content": E.SYSTEM_PROMPT}, {"role": "user", "content": q}],
            "temperature": 0.3, "max_tokens": max_tokens}
    req = urllib.request.Request(E.ENDPOINT, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {E._key()}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://sredaspace.ru", "X-Title": "sreda-173-eval"}, method="POST")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = json.loads(resp.read().decode())
    dt = round(time.time() - t0, 2)
    ch = (raw.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    return dt, (msg.get("content") or "").strip(), ch.get("finish_reason"), raw.get("usage")


for name, qid in RECHECK:
    cat, _, q, note = corpus_by_id[qid]
    for attempt in range(1, 4):
        try:
            dt, content, fr, usage = call_big(model_id[name], q)
            ct = usage.get("completion_tokens") if usage else "?"
            print(f"\n=== {name} {qid} ({dt}s, finish={fr}, compl_tok={ct}) ===")
            print(content[:500] if content else "(ВСЁ РАВНО ПУСТО)")
            break
        except Exception as e:
            print(f"  {name} {qid} попытка {attempt}: {type(e).__name__}: {str(e)[:60]}")
            time.sleep(2)
