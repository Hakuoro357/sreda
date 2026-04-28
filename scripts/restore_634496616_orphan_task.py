"""One-shot recovery: переносит «осиротевшую» task «Найти чек от ноутбука»
из расписания в чек-лист «Глобальные дела на даче» юзера 634496616.

Контекст 2026-04-28 16:26: LLM услышал «запиши в список дел на сегодня
найти чек от ноутбука» как task-with-date вместо checklist-item, создал
task_2228c597423444f582ba6320 в Расписании. Юзеру это в Mini App
не видно как ожидаемое (он ожидал в делах).

Действия:
1. Cancel task task_2228c597423444f582ba6320 (status=cancelled, soft delete)
2. Add «Найти чек от ноутбука» в checklist «Глобальные дела на даче»
   через ChecklistService.add_items (с dedup защитой)
"""
from sreda.db.session import get_db_session
from sreda.db.models.tasks import Task
from sreda.services.checklists import ChecklistService

ORPHAN_TASK_ID = "task_2228c597423444f582ba6320"
TARGET_CHECKLIST_ID = "checklist_7302d7b66011408d8186335b"
ITEM_TITLE = "Найти чек от ноутбука"


def main() -> None:
    sess = next(get_db_session())

    # 1. Cancel orphan task (soft delete — оставляем в БД с status='cancelled'
    # на случай если юзер захочет вернуть; retention позже удалит)
    task = sess.query(Task).filter_by(id=ORPHAN_TASK_ID).first()
    if task is None:
        print(f"WARNING: task {ORPHAN_TASK_ID} not found")
    else:
        print(f"Found task: {task.title!r} status={task.status} date={task.scheduled_date}")
        if task.status == "pending":
            task.status = "cancelled"
            sess.commit()
            print(f"  → cancelled")
        else:
            print(f"  → already {task.status}, skip")

    # 2. Add to checklist
    svc = ChecklistService(sess)
    created, skipped = svc.add_items(
        list_id=TARGET_CHECKLIST_ID,
        items=[ITEM_TITLE],
    )
    if created:
        print(f"Added to checklist: {created[0].id} title={created[0].title!r}")
    elif skipped:
        print(f"Already in checklist (dedup hit): {skipped}")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
