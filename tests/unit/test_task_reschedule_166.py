"""#166 A2 (полный фикс) — перенос задачи: сохранение bot_key напоминания + отсутствие
лишнего пере-создания при неизменённых дате/времени. (tz no-profile→МСК покрыт обновлённым
test_task_reminder_link.test_add_with_reminder_creates_linked_reminder.)"""

from __future__ import annotations

from datetime import date, time

from sreda.db.models.housewife import FamilyReminder
from sreda.services.tasks import TaskService


def _reminder(db_session, rid):
    return db_session.query(FamilyReminder).filter(FamilyReminder.id == rid).one()


def test_reschedule_preserves_reminder_bot_key(db_session):
    """Перенос через TaskService БЕЗ bot_key сохраняет bot_key СТАРОГО напоминания
    (#166: иначе react-сервис сбросил бы новое напоминание на LEGACY_NULL)."""
    svc_owner = TaskService(db_session, bot_key="sreda_home")
    t = svc_owner.add(
        tenant_id="t1", user_id="u1", title="Встреча",
        scheduled_date=date(2026, 4, 24), time_start=time(10, 0),
        reminder_offset_minutes=15)
    old_rid = t.reminder_id
    assert _reminder(db_session, old_rid).bot_key == "sreda_home"

    # перенос ДРУГИМ сервисом без bot_key (как react TaskService(db_session))
    svc_react = TaskService(db_session)
    updated = svc_react.update(
        tenant_id="t1", user_id="u1", task_id=t.id, time_start=time(14, 0))
    assert updated.reminder_id != old_rid  # пере-создано
    assert _reminder(db_session, updated.reminder_id).bot_key == "sreda_home"  # сохранён, не LEGACY


def test_update_same_datetime_does_not_recreate_reminder(db_session):
    """Правка ТЕКСТА с передачей тех же даты/времени НЕ пере-создаёт напоминание
    (#166: schedule_changed только при реальном изменении значения)."""
    svc = TaskService(db_session)
    t = svc.add(
        tenant_id="t1", user_id="u1", title="Встреча",
        scheduled_date=date(2026, 4, 24), time_start=time(10, 0),
        reminder_offset_minutes=15)
    rid = t.reminder_id
    updated = svc.update(
        tenant_id="t1", user_id="u1", task_id=t.id, title="Созвон",
        scheduled_date=date(2026, 4, 24), time_start=time(10, 0))  # те же дата/время
    assert updated.reminder_id == rid, "напоминание НЕ должно пере-создаваться"
    assert updated.title == "Созвон"


def test_update_recurrence_resyncs_reminder(db_session):
    """#166 (R2 Codex high+medium): смена recurrence_rule у задачи с напоминанием
    пере-создаёт напоминание с НОВЫМ rrule (раньше reminder оставался со старым), и bot_key
    сохраняется."""
    svc = TaskService(db_session, bot_key="sreda_home")
    t = svc.add(
        tenant_id="t1", user_id="u1", title="Зарядка",
        scheduled_date=date(2026, 4, 24), time_start=time(8, 0),
        recurrence_rule="FREQ=DAILY", reminder_offset_minutes=10)
    old_rid = t.reminder_id
    assert _reminder(db_session, old_rid).recurrence_rule == "FREQ=DAILY"

    updated = svc.update(
        tenant_id="t1", user_id="u1", task_id=t.id, recurrence_rule="FREQ=WEEKLY")
    assert updated.reminder_id != old_rid  # пере-создано
    new_rem = _reminder(db_session, updated.reminder_id)
    assert new_rem.recurrence_rule == "FREQ=WEEKLY"  # ресинк повторения
    assert new_rem.bot_key == "sreda_home"  # bot_key сохранён


def test_clear_recurrence_makes_reminder_oneshot(db_session):
    """#166 R3: очистка recurrence (="" → None) пере-создаёт напоминание ОДНОРАЗОВЫМ."""
    svc = TaskService(db_session, bot_key="sreda_home")
    t = svc.add(
        tenant_id="t1", user_id="u1", title="Зарядка",
        scheduled_date=date(2026, 4, 24), time_start=time(8, 0),
        recurrence_rule="FREQ=DAILY", reminder_offset_minutes=10)
    old_rid = t.reminder_id
    updated = svc.update(tenant_id="t1", user_id="u1", task_id=t.id, recurrence_rule="")
    assert updated.reminder_id != old_rid
    new_rem = _reminder(db_session, updated.reminder_id)
    assert new_rem.recurrence_rule is None  # стало одноразовым
    assert new_rem.bot_key == "sreda_home"


def test_same_recurrence_does_not_recreate(db_session):
    """#166 R3: тот же rrule → напоминание НЕ пере-создаётся."""
    svc = TaskService(db_session)
    t = svc.add(
        tenant_id="t1", user_id="u1", title="Зарядка",
        scheduled_date=date(2026, 4, 24), time_start=time(8, 0),
        recurrence_rule="FREQ=DAILY", reminder_offset_minutes=10)
    rid = t.reminder_id
    updated = svc.update(
        tenant_id="t1", user_id="u1", task_id=t.id, recurrence_rule="FREQ=DAILY")
    assert updated.reminder_id == rid  # не пере-создано
