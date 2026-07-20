"""Read-only debug: dump all checklist items для указанных чек-листов
(диагностика рассинхрона). Без UPDATE/DELETE.

R1 C9 (2026-07-18): checklist_id'ы больше НЕ захардкожены (были прод-id +
названия списков в публичном репо) — из argv, fail-fast.

Usage:
    python scripts/debug_checklist_items_dump.py <checklist_id> [<checklist_id> ...]
"""
import sys

from sreda.db.session import get_db_session
from sreda.db.models.checklists import ChecklistItem


def main(argv: list[str]) -> int:
    checklist_ids = [a.strip() for a in argv if a.strip()]
    if not checklist_ids:
        raise SystemExit(
            "Usage: debug_checklist_items_dump.py <checklist_id> [<checklist_id> ...]"
        )
    sess = next(get_db_session())
    items = (
        sess.query(ChecklistItem)
        .filter(ChecklistItem.checklist_id.in_(checklist_ids))
        .order_by(ChecklistItem.checklist_id, ChecklistItem.position)
        .all()
    )
    print(f"Total items across {len(checklist_ids)} list(s): {len(items)}\n")
    for it in items:
        print(
            f"{it.checklist_id} | [{it.status}] pos={it.position} "
            f"created={it.created_at} title={it.title!r}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
