"""One-shot recovery: отменяет «осиротевшую» task (LLM услышал checklist-item
как task-with-date) и добавляет её текст в целевой чек-лист.

R1 C9 (2026-07-18): task_id / checklist_id / текст пункта больше НЕ
захардкожены в коде (были прод-id + пользовательский контент в публичном
репо) — читаются из argv с fail-fast.

Usage:
    python scripts/restore_dacha_orphan_task.py <task_id> <checklist_id> <item_title>
"""
import sys

from sreda.db.session import get_db_session
from sreda.db.models.tasks import Task
from sreda.services.checklists import ChecklistService


def main(argv: list[str]) -> int:
    if len(argv) < 3 or not all(a.strip() for a in argv[:3]):
        raise SystemExit(
            "Usage: restore_dacha_orphan_task.py <task_id> <checklist_id> <item_title>"
        )
    orphan_task_id, target_checklist_id, item_title = (
        argv[0].strip(), argv[1].strip(), argv[2].strip(),
    )
    sess = next(get_db_session())

    # 1. Cancel orphan task (soft delete — status='cancelled', retention удалит).
    task = sess.query(Task).filter_by(id=orphan_task_id).first()
    if task is None:
        print(f"WARNING: task {orphan_task_id} not found")
    else:
        print(f"Found task: {task.title!r} status={task.status} date={task.scheduled_date}")
        if task.status == "pending":
            task.status = "cancelled"
            sess.commit()
            print("  -> cancelled")
        else:
            print(f"  -> already {task.status}, skip")

    # 2. Add to checklist (dedup внутри add_items).
    svc = ChecklistService(sess)
    created, skipped = svc.add_items(
        list_id=target_checklist_id,
        items=[item_title],
    )
    if created:
        print(f"Added to checklist: {created[0].id} title={created[0].title!r}")
    elif skipped:
        print(f"Already in checklist (dedup hit): {skipped}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
