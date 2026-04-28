"""Read-only verify задачи task_2228c597423444f582ba6320."""
from sreda.db.session import get_db_session
from sreda.db.models.tasks import Task

sess = next(get_db_session())
t = sess.query(Task).filter_by(id="task_2228c597423444f582ba6320").first()
if t:
    print(f"id={t.id}")
    print(f"  status: {t.status}")
    print(f"  scheduled_date: {t.scheduled_date}")
    print(f"  time_start: {t.time_start} - time_end: {t.time_end}")
    print(f"  recurrence: {t.recurrence_rule}")
    print(f"  title: {t.title!r}")
    print(f"  notes: {t.notes!r}")
    print(f"  tenant_id: {t.tenant_id}")
else:
    print("Task not found")
