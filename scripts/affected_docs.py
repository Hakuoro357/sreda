#!/usr/bin/env python3
"""Определитель задетых архитектурных доков (vex-assistant#248).

Карта «изменённые файлы → задетые enrolled-доки». Пересечение по `Source code` = hard
(док надо синкать по коду-источнику); пересечение ТОЛЬКО по `Related code` = soft
(сигнал-пометка: смежный код тронут — doc-sync агент лишь ФЛАГает «проверь вручную»,
не правит, т.к. это не отслеживаемый источник истины).

Это ОПРЕДЕЛИТЕЛЬ, а не гейт: гейт свежести — `check_docs_freshness.py`. Пустой результат →
синк не запускаем (отсечка).

Использование (из корня репо):
    python scripts/affected_docs.py --range origin/main...HEAD
    python scripts/affected_docs.py --files src/sreda/runtime/react_preflight.py
    git diff --name-only origin/main...HEAD | python scripts/affected_docs.py

Вывод: по строке JSON на задетый док (stdout); сводка — в stderr.
Коды возврата: 0 — норма; 2 — запуск не из корня репо (нет docs/); 3 — сбой `git diff` в `--range`.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))  # docs_registry рядом
from docs_registry import EnrolledDoc, scan_docs  # noqa: E402


def normalize(p: str) -> str:
    """Путь к единому виду: прямые слэши, без ведущего './' и пробелов."""
    p = p.replace("\\", "/").strip()
    return p[2:] if p.startswith("./") else p


def compute_affected(changed: set[str], enrolled: list[EnrolledDoc]) -> list[dict]:
    """Для каждого enrolled-дока: hard-пересечение по source, иначе soft — по related."""
    changed_n = {normalize(c) for c in changed}
    out: list[dict] = []
    for ed in enrolled:
        src = sorted(p for p in (normalize(s) for s in ed.source_paths) if p in changed_n)
        rel = sorted(p for p in (normalize(s) for s in ed.related_paths) if p in changed_n)
        if src:
            out.append({"doc": str(ed.path).replace("\\", "/"), "matched": src,
                        "verified_against": ed.verified_against, "soft": False})
        elif rel:
            out.append({"doc": str(ed.path).replace("\\", "/"), "matched": rel,
                        "verified_against": ed.verified_against, "soft": True})
    return out


def _changed_from_args(args: argparse.Namespace) -> set[str]:
    if args.files:
        return {f for f in args.files if f.strip()}
    if args.range:
        r = subprocess.run(["git", "diff", "--name-only", args.range],
                           capture_output=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:  # сбой git НЕ трактуем как «пустой набор» — иначе тихо пропустим doc-sync
            print(f"FATAL: git diff '{args.range}' упал (rc={r.returncode}): "
                  f"{r.stderr.strip()[:200]}", file=sys.stderr)
            raise SystemExit(3)
        return {ln for ln in r.stdout.splitlines() if ln.strip()}
    return {ln for ln in sys.stdin.read().splitlines() if ln.strip()}


def main(argv: list[str] | None = None) -> int:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # кириллица в выводе под Windows-консолью (cp1251)
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description="Определитель задетых архитектурных доков (#248)")
    g = ap.add_mutually_exclusive_group()  # --files и --range взаимоисключающие (иначе --range молча игнор)
    g.add_argument("--range", help="git-диапазон, напр. origin/main...HEAD")
    g.add_argument("--files", nargs="+", help="явный список изменённых файлов")
    args = ap.parse_args(argv)

    if not pathlib.Path("docs").is_dir():
        print("FATAL: docs/ не найден — запускать из корня репо sreda", file=sys.stderr)
        return 2

    enrolled, _ = scan_docs("docs")
    changed = _changed_from_args(args)
    affected = compute_affected(changed, enrolled)

    for a in affected:
        print(json.dumps(a, ensure_ascii=False))
    n_soft = sum(1 for a in affected if a["soft"])
    print(f"affected_docs: задето {len(affected)} ({len(affected) - n_soft} hard, {n_soft} soft); "
          f"изменённых файлов {len(changed)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
