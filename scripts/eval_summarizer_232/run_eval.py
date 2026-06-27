#!/usr/bin/env python3
"""#232 шаг 0 — прогон моделей-пересказчиков на замороженном корпусе.

Синтетические русские куски истории (corpus_frozen.json) → 3 кандидата →
выжимки + детерминированная оценка must_keep + дамп для ручной сверки.

Запуск (на VDS, env с ключом OpenRouter уже загружен):
  cd /opt/sreda && set -a && . /etc/sreda/.env && set +a
  PYTHONPATH=/opt/sreda/src python /tmp/eval232/run_eval.py
"""
from __future__ import annotations
import json, os, re, sys, time
from pathlib import Path

# sreda на PYTHONPATH (на проде) или локальный src
for p in ("/opt/sreda/src", str(Path(__file__).resolve().parents[2] / "src"), "src"):
    if p not in sys.path and Path(p).exists():
        sys.path.insert(0, p)

from sreda.services.llm import get_chat_llm  # noqa: E402
from langchain_core.messages import SystemMessage, HumanMessage  # noqa: E402

HERE = Path(__file__).resolve().parent
CORPUS = json.load(open(HERE / "corpus_frozen.json", encoding="utf-8"))
META = CORPUS["_meta"]
PROMPT_TMPL = META["summarizer_prompt"]

# Кандидаты. flash-lite — текущий прод (EU-пин). gemma/deepseek — через openrouter.
CANDIDATES = [
    ("gemini-2.5-flash-lite-EU", dict(provider="openrouter-gemini-2.5-flash-lite")),
    ("gemma-4-26b-a4b",          dict(provider="openrouter", model="google/gemma-4-26b-a4b-it")),
    ("deepseek-v4-flash",        dict(provider="openrouter-deepseek")),
    ("mimo-v2.5",                dict(provider="openrouter", model="xiaomi/mimo-v2.5")),
]
# Цена $/1M (вход, выход) — дешёвый приемлемый хост, OpenRouter 2026-06-27.
PRICE = {
    "gemini-2.5-flash-lite-EU": (0.10, 0.40),
    "gemma-4-26b-a4b":          (0.07, 0.34),
    "deepseek-v4-flash":        (0.10, 0.20),
    "mimo-v2.5":                (0.11, 0.28),
}


def norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[^0-9a-zа-я]+", " ", s)
    return " " + re.sub(r"\s+", " ", s).strip() + " "


def hit(fact: str, nsum: str) -> bool:
    return (" " + norm(fact).strip() + " ") in nsum


def build_user(chunk: dict) -> str:
    lines = []
    if chunk.get("prev_summary"):
        lines.append("[Предыдущая выжимка]: " + chunk["prev_summary"])
        lines.append("")
        lines.append("[Новые сообщения]:")
    for m in chunk["history"]:
        who = "Пользователь" if m["role"] == "user" else "Ассистент"
        lines.append(f"{who}: {m['text']}")
    return "\n".join(lines)


def extract_usage(resp):
    um = getattr(resp, "usage_metadata", None)
    if um:
        return {"in": um.get("input_tokens"), "out": um.get("output_tokens")}
    md = getattr(resp, "response_metadata", {}) or {}
    tu = md.get("token_usage") or {}
    return {"in": tu.get("prompt_tokens"), "out": tu.get("completion_tokens")}


def main():
    results = []
    for label, kw in CANDIDATES:
        try:
            llm = get_chat_llm(temperature=0.3, **kw)
        except Exception as e:
            llm = None
            print(f"[{label}] build error: {e!r}")
        cand = {"label": label, "built": llm is not None, "chunks": []}
        if llm is None:
            print(f"[{label}] NOT BUILT (нет ключа/провайдера) — пропуск")
            results.append(cand)
            continue
        for ch in CORPUS["chunks"]:
            sysmsg = PROMPT_TMPL.format(max_chars=ch["max_chars"])
            usr = build_user(ch)
            t0 = time.time()
            content, err, usage, prov = "", None, {}, None
            try:
                resp = llm.invoke([SystemMessage(sysmsg), HumanMessage(usr)])
                content = resp.content or ""
                usage = extract_usage(resp)
                md = getattr(resp, "response_metadata", {}) or {}
                prov = md.get("model_name") or md.get("model")
            except Exception as e:
                err = repr(e)[:300]
            dt = round(time.time() - t0, 2)
            nsum = norm(content)
            mk = ch.get("must_keep", [])
            missed = [f for f in mk if not hit(f, nsum)]
            forbidden = [f for f in ch.get("must_not_contain", []) if hit(f, nsum)]
            cand["chunks"].append({
                "id": ch["id"], "kind": ch["kind"],
                "kept": len(mk) - len(missed), "total": len(mk),
                "missed": missed, "forbidden_present": forbidden,
                "len": len(content), "max_chars": ch["max_chars"],
                "over": len(content) > ch["max_chars"],
                "latency_s": dt, "err": err, "provider": prov, "usage": usage,
                "summary": content,
            })
            tag = "ERR" if err else f"{len(mk)-len(missed)}/{len(mk)}"
            print(f"[{label}] {ch['id']:28s} keep={tag} len={len(content)} {dt}s {err or ''}")
        results.append(cand)

    out = {"frozen": META["frozen_utc"], "candidates": results}
    (HERE / "results.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # markdown для ручной сверки
    md = ["# #232 шаг 0 — результаты прогона пересказчиков\n"]
    md.append("## Сводка\n")
    md.append("| Модель | built | keep% (детерм.) | в рамере | инъекция | ошибки | ~лат. | ~цена/пересказ |")
    md.append("|---|---|---|---|---|---|---|---|")
    for c in results:
        if not c["built"]:
            md.append(f"| {c['label']} | ❌ | — | — | — | — | — | — |")
            continue
        chs = c["chunks"]
        tot = sum(x["total"] for x in chs) or 1
        kept = sum(x["kept"] for x in chs)
        over = sum(1 for x in chs if x["over"])
        errs = sum(1 for x in chs if x["err"])
        inj = [x for x in chs if x["id"] == "c8_injection_adversarial"]
        inj_ok = "✅" if inj and not inj[0]["forbidden_present"] and not inj[0]["err"] else "⚠️"
        lat = round(sum(x["latency_s"] for x in chs) / max(len(chs), 1), 2)
        pin, pout = PRICE.get(c["label"], (0, 0))
        ins = [x["usage"].get("in") or 0 for x in chs]
        outs = [x["usage"].get("out") or 0 for x in chs]
        avg_in = sum(ins) / max(len(ins), 1)
        avg_out = sum(outs) / max(len(outs), 1)
        cost = avg_in / 1e6 * pin + avg_out / 1e6 * pout
        md.append(f"| {c['label']} | ✅ | {kept}/{tot} ({100*kept//tot}%) | {len(chs)-over}/{len(chs)} | {inj_ok} | {errs} | {lat}s | ${cost:.5f} |")
    md.append("\n## Выжимки по кускам (для ручной сверки верности/галлюцинаций)\n")
    for ch in CORPUS["chunks"]:
        md.append(f"### {ch['id']} ({ch['kind']}) — must_keep: {', '.join(ch.get('must_keep', []))}")
        if ch.get("must_not_contain"):
            md.append(f"_must_NOT_contain_: {', '.join(ch['must_not_contain'])}")
        for c in results:
            if not c["built"]:
                continue
            x = next((y for y in c["chunks"] if y["id"] == ch["id"]), None)
            if not x:
                continue
            miss = f" ❌missed: {x['missed']}" if x["missed"] else ""
            fb = f" 🚨forbidden: {x['forbidden_present']}" if x["forbidden_present"] else ""
            md.append(f"- **{c['label']}** ({x['kept']}/{x['total']}{miss}{fb}, {x['len']}c{', OVER' if x['over'] else ''}): {x['err'] or x['summary']}")
        md.append("")
    (HERE / "results.md").write_text("\n".join(md), encoding="utf-8")
    print("\nГОТОВО → results.json + results.md")


if __name__ == "__main__":
    main()
