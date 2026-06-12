# -*- coding: utf-8 -*-
"""#135 — куратор библиотеки планов (авто-approve запрещён планом).

Использование (на VDS, под env):
  python scripts/plan_library_curator.py list [--status candidate]
  python scripts/plan_library_curator.py approve <id> --by boris [--reason ...]
  python scripts/plan_library_curator.py retire <id> --by boris [--reason ...]
  python scripts/plan_library_curator.py anti <id> --by boris --expected-form-file f.json

Показывает ТОЛЬКО форму/теги (PII в таблице нет по построению).
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--status", default="candidate")
    for cmd in ("approve", "retire", "anti"):
        p = sub.add_parser(cmd)
        p.add_argument("entry_id")
        p.add_argument("--by", required=True)
        p.add_argument("--reason", default="")
        if cmd == "anti":
            p.add_argument("--expected-form-file", required=True)
    args = ap.parse_args()

    from sreda.db.models.plan_library import PlanLibraryEntry
    from sreda.db.session import get_session_factory
    session = get_session_factory()()
    try:
        if args.cmd == "list":
            rows = (session.query(PlanLibraryEntry)
                    .filter_by(status=args.status)
                    .order_by(PlanLibraryEntry.created_at.desc())
                    .limit(50).all())
            for r in rows:
                print(f"--- {r.id} [{r.status}] tenant={r.tenant_id} "
                      f"{r.created_at:%Y-%m-%d %H:%M}")
                print(f"    tags: {r.form_tags}")
                print(f"    form: {r.form_json[:400]}")
            print(f"всего: {len(rows)}")
            return 0
        row = session.get(PlanLibraryEntry, args.entry_id)
        if row is None:
            print(f"нет записи {args.entry_id}")
            return 1
        new_status = {"approve": "approved", "retire": "retired",
                      "anti": "anti"}[args.cmd]
        row.status = new_status
        row.decided_at = datetime.now(timezone.utc)
        row.decided_by = args.by
        row.decision_reason = args.reason or None
        if args.cmd == "anti":
            with open(args.expected_form_file, encoding="utf-8") as f:
                row.expected_form_json = json.dumps(
                    json.load(f), ensure_ascii=False, sort_keys=True)
        session.commit()
        print(f"{args.entry_id} → {new_status} (by {args.by})")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
