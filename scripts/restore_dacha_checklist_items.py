"""One-shot recovery: восстанавливает пропавшие пункты в чек-листе (LLM
отрапортовал «добавила», но не вызвал add_checklist_items).

R1 C9 (2026-07-18): checklist_id и тексты пунктов больше НЕ захардкожены
(были прод-id + пользовательский контент в публичном репо) — из argv,
fail-fast.

Usage:
    python scripts/restore_dacha_checklist_items.py <checklist_id> <item1> [item2 ...]
"""
import sys

from sreda.db.session import get_db_session
from sreda.services.checklists import ChecklistService


def main(argv: list[str]) -> int:
    if len(argv) < 2 or not argv[0].strip() or not any(a.strip() for a in argv[1:]):
        raise SystemExit(
            "Usage: restore_dacha_checklist_items.py <checklist_id> <item1> [item2 ...]"
        )
    checklist_id = argv[0].strip()
    items = [a.strip() for a in argv[1:] if a.strip()]

    sess = next(get_db_session())
    svc = ChecklistService(sess)
    print(f"Restoring {len(items)} items to {checklist_id}...")
    created, skipped = svc.add_items(list_id=checklist_id, items=items)
    print(f"Created: {len(created)}")
    for it in created:
        print(f"  + {it.id} pos={it.position} title={it.title!r}")
    if skipped:
        print(f"Skipped (dedup, already exist): {len(skipped)}")
        for t in skipped:
            print(f"  - {t!r}")
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
