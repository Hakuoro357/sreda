#!/usr/bin/env python3
"""Проверка актуальности архитектурных доков (vex-assistant#248, анти doc-drift).

Док считается ENROLLED (отслеживаемым), если в шапке есть ОБА поля:
    **Source code:** `путь/к/файлу.py` [, `ещё/файл.py` ...]
    **Verified-against:** `<git-sha>`   (коммит, на котором док подтверждён актуальным)

Для каждого enrolled-дока: если хоть один файл из `Source code` менялся в git ПОСЛЕ
`Verified-against` → док STALE (код ушёл вперёд, док мог устареть). Тогда нужно
перечитать док против кода, поправить при необходимости и обновить `Verified-against`
на текущий коммит.

Коды возврата: 0 — все enrolled-доки свежие; 1 — есть STALE/ошибки; 2 — запуск не из корня репо.
Доки с `Source code`, но без `Verified-against` — НЕ отслеживаются (печатаются как «не enrolled»,
проверку не валят) — это путь постепенного бэкфилла.

Разбор шапок — в общем модуле `docs_registry` (одна карта файл→док для freshness и affected_docs).
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))  # docs_registry рядом
from docs_registry import scan_docs  # noqa: E402

DOCS_DIR = pathlib.Path("docs")


def git(*args: str) -> subprocess.CompletedProcess:
    # encoding=utf-8 явно: git отдаёт UTF-8, а локаль Windows (cp1251) превратила бы сообщения в мусор.
    return subprocess.run(["git", *args], capture_output=True, encoding="utf-8", errors="replace")


def main() -> int:
    for _stream in (sys.stdout, sys.stderr):  # UTF-8 под Windows-консолью (cp1251) — иначе кириллица ронит print
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001 — не TTY/старый Python: оставляем как есть
            pass
    if not DOCS_DIR.is_dir():
        print("FATAL: docs/ не найден — запускать из корня репо sreda", file=sys.stderr)
        return 2

    enrolled, not_enrolled = scan_docs(DOCS_DIR)

    stale: list[tuple[pathlib.Path, str, list[str], list[str]]] = []
    errors: list[tuple[pathlib.Path, str]] = []

    for ed in enrolled:
        ver = ed.verified_against
        paths = list(ed.source_paths)
        if git("cat-file", "-e", f"{ver}^{{commit}}").returncode != 0:
            errors.append((ed.path, f"Verified-against `{ver}` не найден в истории git"))
            continue
        missing = [p for p in paths if not pathlib.Path(p).exists()]
        if missing:
            errors.append((ed.path, f"Source code-файлы не существуют: {', '.join(missing)}"))
            continue
        r = git("log", "--oneline", f"{ver}..HEAD", "--", *paths)
        changed = [ln for ln in r.stdout.splitlines() if ln.strip()]
        if changed:
            stale.append((ed.path, ver, paths, changed))

    # ── отчёт ──
    print(f"docs-freshness: enrolled={len(enrolled)}, не-enrolled(с Source code)={len(not_enrolled)}")
    for md in not_enrolled:
        print(f"  · не отслеживается (нет Verified-against): {md}")

    for md, why in errors:
        print(f"  ✗ ОШИБКА {md}: {why}")

    for md, ver, paths, changed in stale:
        print(f"  ✗ STALE {md}")
        print(f"      source {', '.join(paths)} менялся после Verified-against `{ver}`: {len(changed)} коммит(ов)")
        for ln in changed[:3]:
            print(f"        {ln}")
        print("      → перечитай док против кода, поправь, обнови Verified-against на текущий HEAD")

    if stale or errors:
        print(f"docs-freshness: ПРОВАЛ — {len(stale)} stale, {len(errors)} ошибок")
        return 1
    print("docs-freshness: OK — все enrolled-доки свежие")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
