"""One-shot debug: dump checklists для тенанта.

Использовался для исследования рассинхрона в Mini App (инцидент 2026-04-28).
Не committable — артефакт расследования.

Tenant id — PII, НЕ коммитится (audit 2026-07-18): передаётся аргументом.

Usage: python scripts/debug_checklists_dump.py <tenant_id>
"""
import sys

from sreda.db.session import get_db_session
from sreda.db.models.checklists import Checklist, ChecklistItem


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        raise SystemExit("Usage: debug_checklists_dump.py <tenant_id>")
    tenant_id = sys.argv[1].strip()
    sess = next(get_db_session())
    lists = (
        sess.query(Checklist)
        .filter_by(tenant_id=tenant_id)
        .order_by(Checklist.created_at)
        .all()
    )
    print(f"Found {len(lists)} checklists for {tenant_id}")
    print()
    for cl in lists:
        print(f"id={cl.id}")
        print(f"  status: {cl.status}")
        print(f"  created_at: {cl.created_at}")
        print(f"  title: {cl.title!r}")
        items = (
            sess.query(ChecklistItem)
            .filter_by(checklist_id=cl.id)
            .order_by(ChecklistItem.position)
            .all()
        )
        for it in items:
            print(
                f"    [{it.status}] pos={it.position} "
                f"created={it.created_at} title={it.title!r}"
            )
        print()


if __name__ == "__main__":
    main()
