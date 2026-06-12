# -*- coding: utf-8 -*-
"""#135 — канарейка форм планов (v1: компаратор; --live/--ab — срез 4).

Вход: JSON-файл кейсов [{"name", "raw_text" (ответ планировщика),
"expected_form" (эталон) | "bad_form" (анти)}]. Прогон: НАСТОЯЩИЙ парс
Plan + project_plan_form → сравнение с эталоном. Любое расхождение →
exit 1 + diff-отчёт (сравнивается ФОРМА, не текст — план #135/g-040).
"""
from __future__ import annotations

import argparse
import json
import sys


def form_of(raw_text: str) -> dict:
    from sreda.runtime.planner.plan_library import project_plan_form
    from sreda.runtime.planner.schemas import Plan
    plan = Plan.model_validate(json.loads(raw_text))
    return project_plan_form(plan)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cases_file")
    args = ap.parse_args()
    with open(args.cases_file, encoding="utf-8") as f:
        cases = json.load(f)
    failures = 0
    for case in cases:
        name = case.get("name", "?")
        try:
            got = form_of(case["raw_text"])
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {name}: парс/проекция упали: {exc}")
            failures += 1
            continue
        expected = case.get("expected_form")
        bad = case.get("bad_form")
        if expected is not None and got != expected:
            print(f"[FAIL] {name}: форма разошлась с эталоном")
            print(f"  ожидалось: {json.dumps(expected, ensure_ascii=False)[:300]}")
            print(f"  получено:  {json.dumps(got, ensure_ascii=False)[:300]}")
            failures += 1
        elif bad is not None and got == bad:
            print(f"[FAIL] {name}: форма совпала с известно-плохой (анти)")
            failures += 1
        else:
            print(f"[ok] {name}")
    print(f"итог: {len(cases) - failures}/{len(cases)} ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
