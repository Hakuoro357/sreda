"""Read-only verify задачи по id.

R1 C9 (2026-07-18): task_id больше НЕ захардкожен (был прод-id в коде И в
имени файла debug_task_2228c597.py — публичный репо) — из argv, fail-fast.

Usage:
    python scripts/debug_task.py <task_id>
"""
import sys

from sreda.db.session import get_db_session
from sreda.db.models.tasks import Task


def main(argv: list[str]) -> int:
    if not argv or not argv[0].strip():
        raise SystemExit("Usage: debug_task.py <task_id>")
    task_id = argv[0].strip()
    sess = next(get_db_session())
    t = sess.query(Task).filter_by(id=task_id).first()
    if not t:
        print("Task not found")
        return 0
    print(f"id={t.id}")
    print(f"  status: {t.status}")
    print(f"  scheduled_date: {t.scheduled_date}")
    print(f"  time_start: {t.time_start} - time_end: {t.time_end}")
    print(f"  recurrence: {t.recurrence_rule}")
    print(f"  title: {t.title!r}")
    print(f"  notes: {t.notes!r}")
    print(f"  tenant_id: {t.tenant_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
