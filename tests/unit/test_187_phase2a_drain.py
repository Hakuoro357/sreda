"""#187 Phase 2a — soft_delete_tenant drain + worker fencing/producer-filters.

RED-first (TDD) tests for the acceptance-checklist items the phase covers:

- **A6 (drain)** — ``soft_delete_tenant`` flips ``deleted_at`` and actively
  drains pending artefacts to terminal states with a reason:
    * outbox pending → ``status='dropped'`` + ``drop_reason='tenant_deleted'``
    * message_jobs pending/processing → ``status='done'`` (direct UPDATE) +
      ``finished_at`` set + ``last_error='tenant_deleted'``
    * inbound_events non-terminal (new/needs_classification/classified) →
      ``status='skipped'`` + ``status_reason='tenant_deleted'``
  family_reminders are NOT drained (R1 MAJOR anti-resurrection: a drained
  terminal ``fired`` could be revived to pending via ``snooze()``). The
  producer-filter ``due_now`` (deleted_at IS NULL) + fencing keep a deleted
  tenant's reminders from firing — see the A5 test.
  Idempotent: a second call on an already-deleted tenant is a no-op.

- **A5 (outgoing / state)** — a deleted tenant:
    * outbox ``_process_one`` → ``dropped``, ``send`` NOT called
    * reminder worker → due reminder NOT delivered AND ``next_trigger_at``
      NOT advanced (producer-filter ``due_now`` + per-row fencing recheck)

- **anti-over-reach** — message_jobs already terminal (done/failed) and rows
  of OTHER tenants are untouched; a recurring reminder is NOT touched by drain.

Field/status names below were confirmed against the live models
(``db/models/{core,message_jobs,inbound_event,housewife}.py``):
- ``MessageJob.status`` enum: pending/processing/done/failed/dead_letter;
  CHECK requires ``finished_at IS NOT NULL`` for terminal → drain sets it.
- ``InboundEvent.status``: new/needs_classification/classified/consumed/skipped;
  has ``status_reason`` String(128).
- ``FamilyReminder.status``: pending/fired/cancelled; ``recurrence_rule`` NULL =
  one-shot. Drain НЕ трогает напоминания (фолд R1 убрал one-shot drain — снуз мог бы
  воскресить ``fired``). Доставку удалённого тенанта режут producer-фильтр ``due_now``
  + fencing-recheck воркера; window-drain — Фаза 3 (restore).
- ``OutboxMessage``: ``status`` default pending; ``drop_reason`` String(64).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from sreda.db.models.core import OutboxMessage, Tenant
from sreda.db.models.housewife import FamilyReminder
from sreda.db.models.inbound_event import InboundEvent
from sreda.db.models.message_jobs import MessageJob
from sreda.services.tenant_lifecycle import is_tenant_active, soft_delete_tenant

from tests.unit.conftest import seed_telegram_user


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _seed_tenant(
    session: Session,
    *,
    tenant_id: str,
    chat_id: str,
    user_id: str,
) -> None:
    seed_telegram_user(
        session,
        tenant_id=tenant_id,
        chat_id=chat_id,
        user_id=user_id,
        workspace_id=f"ws_{tenant_id}",
        profile=False,
    )


def _add_outbox(
    session: Session,
    *,
    tenant_id: str,
    workspace_id: str,
    status: str = "pending",
    drop_reason: str | None = None,
) -> OutboxMessage:
    row = OutboxMessage(
        id=f"out_{uuid4().hex[:20]}",
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_type="telegram",
        status=status,
        payload_json="{}",
        drop_reason=drop_reason,
        bot_key="sreda",
    )
    session.add(row)
    return row


def _add_message_job(
    session: Session,
    *,
    tenant_id: str,
    status: str,
    job_id: str | None = None,
    external_update_id: str | None = None,
) -> MessageJob:
    now = _now()
    started_at = None
    finished_at = None
    lease_expires_at = None
    if status == "processing":
        started_at = now
        lease_expires_at = now + timedelta(seconds=60)
    elif status in ("done", "failed", "dead_letter"):
        started_at = now
        finished_at = now
    row = MessageJob(
        id=job_id or f"job_{uuid4().hex[:16]}",
        tenant_id=tenant_id,
        thread_id=f"thread_{tenant_id}",
        channel="telegram",
        external_update_id=external_update_id or uuid4().hex[:12],
        message_payload={"text": "hi"},
        status=status,
        enqueued_at=now,
        started_at=started_at,
        finished_at=finished_at,
        lease_expires_at=lease_expires_at,
        attempt=1 if status != "pending" else 0,
    )
    session.add(row)
    return row


def _add_inbound_event(
    session: Session,
    *,
    tenant_id: str,
    status: str,
    event_id: str | None = None,
) -> InboundEvent:
    row = InboundEvent(
        id=event_id or f"inb_{uuid4().hex[:16]}",
        tenant_id=tenant_id,
        feature_key="eds_monitor",
        event_type="claim_updated",
        external_event_key=uuid4().hex[:20],
        payload_json="{}",
        relevance_score=0.9,
        status=status,
    )
    session.add(row)
    return row


def _add_reminder(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    recurrence_rule: str | None,
    next_trigger_at: datetime,
    status: str = "pending",
    reminder_id: str | None = None,
) -> FamilyReminder:
    row = FamilyReminder(
        id=reminder_id or f"rem_{uuid4().hex[:16]}",
        tenant_id=tenant_id,
        user_id=user_id,
        title="принять лекарство",
        trigger_at=next_trigger_at,
        next_trigger_at=next_trigger_at,
        recurrence_rule=recurrence_rule,
        status=status,
        bot_key="sreda",
    )
    session.add(row)
    return row


# ---------------------------------------------------------------------------
# A6 — drain
# ---------------------------------------------------------------------------


def test_soft_delete_drains_all_pending(db_session: Session) -> None:
    """A6: soft_delete sets deleted_at and drains every pending artefact."""
    tid = "tenant_del"
    _seed_tenant(db_session, tenant_id=tid, chat_id="111", user_id="u_del")
    past = _now() - timedelta(minutes=1)

    outbox = _add_outbox(db_session, tenant_id=tid, workspace_id=f"ws_{tid}")
    job_pending = _add_message_job(db_session, tenant_id=tid, status="pending")
    job_processing = _add_message_job(db_session, tenant_id=tid, status="processing")
    inb_new = _add_inbound_event(db_session, tenant_id=tid, status="new")
    inb_needs = _add_inbound_event(
        db_session, tenant_id=tid, status="needs_classification"
    )
    inb_classified = _add_inbound_event(
        db_session, tenant_id=tid, status="classified"
    )
    # one-shot due reminder — drain MUST NOT touch it (R1 MAJOR anti-resurrection:
    # a drained terminal `fired` could be revived to pending via snooze()).
    # Producer-filter due_now (deleted_at IS NULL) + fencing keep it from firing.
    one_shot = _add_reminder(
        db_session,
        tenant_id=tid,
        user_id="u_del",
        recurrence_rule=None,
        next_trigger_at=past,
    )
    db_session.commit()

    assert is_tenant_active(db_session, tid) is True
    soft_delete_tenant(db_session, tid)

    db_session.refresh(outbox)
    db_session.refresh(job_pending)
    db_session.refresh(job_processing)
    db_session.refresh(inb_new)
    db_session.refresh(inb_needs)
    db_session.refresh(inb_classified)
    db_session.refresh(one_shot)
    tenant = db_session.get(Tenant, tid)

    # Flag set.
    assert tenant.deleted_at is not None
    assert is_tenant_active(db_session, tid) is False

    # outbox → dropped + reason
    assert outbox.status == "dropped"
    assert outbox.drop_reason == "tenant_deleted"

    # message_jobs → done + finished_at + last_error
    for job in (job_pending, job_processing):
        assert job.status == "done"
        assert job.finished_at is not None
        assert job.last_error == "tenant_deleted"

    # inbound_events → skipped + reason
    for ev in (inb_new, inb_needs, inb_classified):
        assert ev.status == "skipped"
        assert ev.status_reason == "tenant_deleted"

    # one-shot due reminder — NOT touched by drain (R1 MAJOR anti-resurrection).
    # Stays pending with its trigger intact; the producer-filter (A5 test) is
    # what prevents the deleted tenant's reminder from ever being delivered.
    assert one_shot.status == "pending"
    assert one_shot.next_trigger_at is not None


def test_soft_delete_is_idempotent(db_session: Session) -> None:
    """A6 idempotency: a second call on a deleted tenant is a no-op."""
    tid = "tenant_idem"
    _seed_tenant(db_session, tenant_id=tid, chat_id="222", user_id="u_idem")
    outbox = _add_outbox(db_session, tenant_id=tid, workspace_id=f"ws_{tid}")
    db_session.commit()

    soft_delete_tenant(db_session, tid)
    db_session.refresh(outbox)
    first_deleted_at = db_session.get(Tenant, tid).deleted_at
    assert outbox.status == "dropped"

    # A fresh pending row added AFTER deletion must NOT be re-drained by a
    # second soft_delete call (idempotent = no-op when already deleted).
    late_row = _add_outbox(db_session, tenant_id=tid, workspace_id=f"ws_{tid}")
    db_session.commit()

    soft_delete_tenant(db_session, tid)  # must not raise, must not re-drain
    db_session.refresh(late_row)
    # deleted_at unchanged (not re-stamped)
    assert db_session.get(Tenant, tid).deleted_at == first_deleted_at
    # the late row stays pending — second call was a no-op
    assert late_row.status == "pending"


def test_soft_delete_does_not_touch_terminal_or_other_tenant(
    db_session: Session,
) -> None:
    """anti-over-reach: terminal message_jobs, other-tenant rows, and BOTH a
    recurring AND a one-shot reminder are NOT touched by the drain (R1 MAJOR —
    reminders are no longer drained at all; producer-filter + Phase 3 cover
    them)."""
    tid = "tenant_a"
    other = "tenant_b"
    _seed_tenant(db_session, tenant_id=tid, chat_id="333", user_id="u_a")
    _seed_tenant(db_session, tenant_id=other, chat_id="444", user_id="u_b")
    past = _now() - timedelta(minutes=1)

    # already-terminal jobs of the deleted tenant — must stay as-is
    job_done = _add_message_job(db_session, tenant_id=tid, status="done")
    job_failed = _add_message_job(db_session, tenant_id=tid, status="failed")

    # other tenant's pending rows — must NOT be drained
    other_outbox = _add_outbox(
        db_session, tenant_id=other, workspace_id=f"ws_{other}"
    )
    other_job = _add_message_job(db_session, tenant_id=other, status="pending")
    other_inb = _add_inbound_event(db_session, tenant_id=other, status="new")

    # recurring reminder of the deleted tenant — NOT touched (producer-filter
    # + Phase 3 restore handle it; drain no longer touches reminders at all)
    recurring = _add_reminder(
        db_session,
        tenant_id=tid,
        user_id="u_a",
        recurrence_rule="FREQ=DAILY;BYHOUR=9",
        next_trigger_at=past,
    )
    # one-shot due reminder of the deleted tenant — ALSO NOT touched by drain
    # (R1 MAJOR anti-resurrection: drain must leave both kinds intact).
    one_shot = _add_reminder(
        db_session,
        tenant_id=tid,
        user_id="u_a",
        recurrence_rule=None,
        next_trigger_at=past,
    )
    db_session.commit()

    soft_delete_tenant(db_session, tid)

    for row in (
        job_done,
        job_failed,
        other_outbox,
        other_job,
        other_inb,
        recurring,
        one_shot,
    ):
        db_session.refresh(row)

    # terminal jobs of deleted tenant untouched
    assert job_done.status == "done"
    assert job_done.last_error != "tenant_deleted"
    assert job_failed.status == "failed"

    # other tenant fully intact
    assert other_outbox.status == "pending"
    assert other_outbox.drop_reason is None
    assert other_job.status == "pending"
    assert other_inb.status == "new"

    # recurring reminder untouched by drain (drain no longer touches reminders)
    assert recurring.status == "pending"
    assert recurring.next_trigger_at is not None  # NOT cleared
    assert recurring.recurrence_rule == "FREQ=DAILY;BYHOUR=9"

    # one-shot reminder ALSO untouched by drain (R1 MAJOR anti-resurrection)
    assert one_shot.status == "pending"
    assert one_shot.next_trigger_at is not None  # NOT fired, NOT cleared
    assert one_shot.recurrence_rule is None


# ---------------------------------------------------------------------------
# A5 — outgoing cut + state not advanced
# ---------------------------------------------------------------------------


class _FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text})
        return {"ok": True, "result": {"message_id": 1, "date": 1}}


def test_deleted_tenant_outbox_process_one_drops_without_send(
    db_session: Session,
) -> None:
    """A5: a pending outbox row of a deleted tenant is dropped by
    ``_process_one`` and the telegram send is NOT invoked."""
    from sreda.workers.outbox_delivery import OutboxDeliveryWorker

    tid = "tenant_out"
    _seed_tenant(db_session, tenant_id=tid, chat_id="42", user_id="u_out")
    row = _add_outbox(db_session, tenant_id=tid, workspace_id=f"ws_{tid}")
    db_session.commit()

    # Delete AFTER the row was enqueued — simulates in-flight outbox.
    soft_delete_tenant(db_session, tid)
    db_session.refresh(row)
    # The drain itself already dropped it; reset to pending to prove the
    # worker-level fence independently (a row could be enqueued by a racing
    # producer between drain and the worker tick).
    row.status = "pending"
    row.drop_reason = None
    db_session.commit()

    fake_tg = _FakeTelegram()
    # #138 Ф2: воркер не хранит self.session; _process_one берёт session
    # параметром — зовём helper напрямую с db_session (seam не задействован).
    worker = OutboxDeliveryWorker(telegram_client=fake_tg)
    asyncio.run(worker._process_one(db_session, row, now_utc=_now()))

    db_session.refresh(row)
    assert row.status == "dropped"
    assert row.drop_reason == "tenant_deleted"
    assert fake_tg.sent == []  # nothing sent


def test_deleted_tenant_reminder_not_delivered_and_not_advanced(
    worker_db: Session,
) -> None:
    """A5 (producer-filter): a due reminder of a deleted tenant is excluded by
    ``due_now`` (JOIN tenants AND deleted_at IS NULL) → NOT delivered and its
    ``next_trigger_at`` is NOT advanced.

    Since drain no longer touches reminders (R1 MAJOR), the reminder stays
    pending after delete; the producer-filter is the line of defence the worker
    actually exercises on the normal path. The deeper fencing recheck (the
    in-tick window between SELECT and advance) is covered separately in
    ``test_deleted_tenant_reminder_fencing_recheck_skips_without_advance``.

    #138 Ф2: воркер сам открывает seam-сессии → фикстура ``worker_db`` (сид
    коммитим ДО прогона; ``expire_all`` перед ассертом на засиженную строку)."""
    from sreda.workers.housewife_reminder_worker import HousewifeReminderWorker

    tid = "tenant_rem"
    _seed_tenant(worker_db, tenant_id=tid, chat_id="555", user_id="u_rem")
    past = _now() - timedelta(minutes=1)
    reminder = _add_reminder(
        worker_db,
        tenant_id=tid,
        user_id="u_rem",
        recurrence_rule=None,
        next_trigger_at=past,
    )
    worker_db.commit()

    # Soft-delete the tenant. Drain leaves the reminder pending+due; the
    # producer-filter in due_now is what keeps the worker from picking it up.
    soft_delete_tenant(worker_db, tid)
    worker_db.commit()
    worker_db.refresh(reminder)
    assert reminder.status == "pending"  # drain did NOT touch it (R1 MAJOR)

    fired = asyncio.run(HousewifeReminderWorker().process_pending(now=_now()))

    worker_db.expire_all()
    reminder = worker_db.get(FamilyReminder, reminder.id)
    assert fired == 0
    # No outbox row was produced for the deleted tenant.
    out_count = (
        worker_db.query(OutboxMessage)
        .filter(OutboxMessage.tenant_id == tid)
        .count()
    )
    assert out_count == 0
    # State NOT advanced — still pending, still due at the same time.
    # NB: SQLite DateTime(timezone=True) strips tzinfo on store; compare on the
    # naive wall-clock value (the point is "not advanced", tz round-trip aside).
    assert reminder.status == "pending"
    stored = reminder.next_trigger_at
    if stored.tzinfo is not None:
        stored = stored.replace(tzinfo=None)
    assert stored == past.replace(tzinfo=None)


def test_deleted_tenant_reminder_fencing_recheck_skips_without_advance(
    worker_db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A5 (fencing recheck — non-vacuous): proves the WORKER's in-tick
    ``is_tenant_active`` recheck branch actually runs.

    On the normal path the producer-filter ``due_now`` already excludes a
    deleted tenant, so the per-row fencing ``continue`` (worker line "tenant
    удалён (fencing) — пропуск без advance") is never reached. To exercise it
    we simulate the real race window: ``due_now`` selected the reminder while
    the tenant was still active, THEN an admin soft-deleted the tenant mid-tick
    in another process.

    #138 Ф2: воркер сам открывает seam-сессии, поэтому у него больше НЕТ
    ``worker.service`` — скан зовёт ``HousewifeReminderService(scan).due_now`` в
    ``privileged_session``, а fencing ``is_tenant_active`` идёт под
    ``tenant_session`` семьи. Патчим методы НА КЛАССЕ
    ``HousewifeReminderService`` (не на экземпляре — воркер строит его сам
    внутри): ``due_now`` возвращает напоминание удалённого тенанта (иначе
    producer-фильтр отсеял бы), ``mark_fired`` — шпион, доказывающий, что
    fencing ``continue`` сработал ДО advance."""
    from sreda.services.housewife_reminders import HousewifeReminderService
    from sreda.workers.housewife_reminder_worker import HousewifeReminderWorker

    tid = "tenant_fence"
    _seed_tenant(worker_db, tenant_id=tid, chat_id="666", user_id="u_fence")
    past = _now() - timedelta(minutes=1)
    reminder = _add_reminder(
        worker_db,
        tenant_id=tid,
        user_id="u_fence",
        recurrence_rule=None,
        next_trigger_at=past,
    )
    worker_db.commit()

    # Mark the tenant deleted AFTER it would have been selected — the reminder
    # itself stays pending+due (drain doesn't touch reminders). Commit so the
    # worker's own seam-sessions (separate connections) observe the deletion.
    soft_delete_tenant(worker_db, tid)
    worker_db.commit()
    worker_db.refresh(reminder)
    assert reminder.status == "pending"

    # Snapshot the id+tenant_id the scan needs BEFORE any expire, so the patched
    # due_now returns plain values (no lazy reload on a detached row).
    rem_id = reminder.id
    rem_tenant = reminder.tenant_id

    # Force due_now to YIELD the reminder despite the deleted tenant — the only
    # way to drive the worker into the fencing recheck branch (the real
    # producer-filter would have filtered it out). Patch the CLASS method: the
    # worker constructs its own service inside the privileged scan block. The
    # scan reads only ``.id`` / ``.tenant_id`` off each row, so a tiny stand-in
    # carrying those two fields is enough and avoids cross-session coupling.
    class _DueRow:
        id = rem_id
        tenant_id = rem_tenant

    monkeypatch.setattr(
        HousewifeReminderService,
        "due_now",
        lambda self, *, now=None, limit=50: [_DueRow()],
    )

    # Spy on mark_fired (class-level) to assert state is NOT advanced (worker
    # must `continue` BEFORE calling it). ``self`` is the service instance the
    # worker builds under tenant_session.
    advanced: list = []
    real_mark_fired = HousewifeReminderService.mark_fired

    def _spy_mark_fired(self, rem, *, now=None):  # noqa: ANN001
        advanced.append(rem.id)
        return real_mark_fired(self, rem, now=now)

    monkeypatch.setattr(HousewifeReminderService, "mark_fired", _spy_mark_fired)

    fired = asyncio.run(HousewifeReminderWorker().process_pending(now=_now()))

    worker_db.expire_all()
    reminder = worker_db.get(FamilyReminder, rem_id)
    assert fired == 0
    assert advanced == []  # fencing `continue` hit BEFORE mark_fired → no advance
    # No outbox row produced for the deleted tenant.
    out_count = (
        worker_db.query(OutboxMessage)
        .filter(OutboxMessage.tenant_id == tid)
        .count()
    )
    assert out_count == 0
    # State NOT advanced — still pending, still due at the same time.
    assert reminder.status == "pending"
    stored = reminder.next_trigger_at
    if stored.tzinfo is not None:
        stored = stored.replace(tzinfo=None)
    assert stored == past.replace(tzinfo=None)
