"""#187 Phase 3 — restore_tenant + restore-window drain (anti-resurrection).

RED-first (TDD) tests for the acceptance-checklist items the phase covers:

- **A2 (restore не воскрешает)** — ``restore_tenant`` clears ``deleted_at`` AND
  drains the deletion window ``[deleted_at, restored_at]`` so nothing stale fires
  the instant the tenant comes back. 🔴 The window is BOUNDED ON BOTH ENDS — only
  artefacts whose trigger / appearance fell INSIDE ``[deleted_at, restored_at]``
  are drained; an artefact already overdue BEFORE the deletion (``< deleted_at``)
  is NOT touched (it is not a product of the deletion). Per kind:
    * one-shot reminders (``recurrence_rule IS NULL``, pending,
      ``deleted_at <= next_trigger_at <= restored_at``) → terminal ``cancelled``
      (not pending — overdue during the window, no longer relevant). ``cancelled``
      is the SAFE terminal — ``fired`` could be revived by ``snooze()`` which
      unconditionally sets ``status='pending'``.
    * recurring reminders (``recurrence_rule`` set, pending,
      ``deleted_at <= next_trigger_at <= restored_at``) → ``next_trigger_at``
      advanced to the next occurrence strictly AFTER ``restored_at`` (missed fires
      during the window are NOT delivered; with MANY missed occurrences the result
      is the FIRST occurrence after restore, not a single step).
    * inbound_events non-terminal whose ``created_at`` OR ``classified_at`` is in
      ``[deleted_at, restored_at]`` → ``status='skipped'`` +
      ``status_reason='tenant_deleted'`` (symmetric to one-shot; else
      ``list_ready_for_delivery`` would revive a stale proactive after restore).
  After restore, running the reminder worker delivers NOTHING stale (outbox empty
  / no send).

- **A8 (обратимость + идемпотентность)** — soft_delete → restore → tenant is
  active again (doors let it through); a second restore on an active tenant is a
  no-op (returns False); drained/advanced rows are NOT resurrected.

- **compute_next_occurrence_after** — the extracted shared helper returns the
  next occurrence strictly after ``reference``; the two pre-existing call-sites
  (``mark_fired`` / ``acknowledge``) still produce the same result they did
  inline.

Field/status names below were confirmed against the live models:
- ``FamilyReminder.status`` enum (db/models/housewife.py:57-59): pending / fired /
  cancelled. ``recurrence_rule`` NULL = one-shot. ``cancel()`` sets
  ``next_trigger_at=None`` (housewife_reminders.py:455) — restore one-shot drain
  mirrors that terminal shape.
- ``InboundEvent`` (db/models/inbound_event.py): ``created_at`` NOT NULL
  (default=_utcnow, line 92-94); ``classified_at`` NULLABLE (line 95-97) — a
  ``status='new'`` row has ``classified_at IS NULL`` so the classified_at range
  predicate silently does NOT match it; it is caught only by the ``created_at``
  range. ``status`` / ``status_reason`` non-terminal = new/needs_classification/
  classified.
- rrule logic lived inline in ``mark_fired`` (housewife_reminders.py:534-539) AND
  ``acknowledge`` (housewife_reminders.py:574-579) — identical
  ``rrulestr(rule, dtstart=trigger_at).after(reference, inc=False)`` — extracted
  into ``compute_next_occurrence_after``.

Window-control technique (deterministic): in production ``deleted_at`` is set to
``now`` inside ``soft_delete_tenant`` and ``restore_tenant`` runs microseconds
later — too narrow to land a fixture "inside" by timing. So after
``soft_delete_tenant`` the test pins ``tenant.deleted_at`` to a controlled past
anchor via ``_force_deleted_at`` (which commits), widening the window to
``[anchor, now]`` and letting fixtures be placed in / before / after it
deterministically.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models.core import OutboxMessage, Tenant
from sreda.db.models.housewife import FamilyReminder
from sreda.db.models.inbound_event import InboundEvent
from sreda.services.housewife_reminders import (
    HousewifeReminderService,
    compute_next_occurrence_after,
)
from sreda.services.tenant_lifecycle import (
    is_tenant_active,
    restore_tenant,
    soft_delete_tenant,
)

from tests.unit.conftest import seed_telegram_user


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Seeding helpers (mirror test_187_phase2a_drain.py)
# ---------------------------------------------------------------------------


def _seed_tenant(
    session: Session, *, tenant_id: str, chat_id: str, user_id: str
) -> None:
    seed_telegram_user(
        session,
        tenant_id=tenant_id,
        chat_id=chat_id,
        user_id=user_id,
        workspace_id=f"ws_{tenant_id}",
        profile=False,
    )


def _force_deleted_at(session: Session, tenant_id: str, when: datetime) -> None:
    """Pin ``tenant.deleted_at`` to *when* and commit.

    Used AFTER ``soft_delete_tenant`` to set a controlled lower window bound so
    fixtures can be placed deterministically inside / before the window. The
    tenant stays soft-deleted (``deleted_at`` is non-NULL), so the subsequent
    ``restore_tenant`` still performs its drain.
    """
    tenant = session.get(Tenant, tenant_id)
    assert tenant is not None
    tenant.deleted_at = when
    session.commit()


def _add_reminder(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    recurrence_rule: str | None,
    trigger_at: datetime,
    next_trigger_at: datetime,
    status: str = "pending",
    reminder_id: str | None = None,
) -> FamilyReminder:
    row = FamilyReminder(
        id=reminder_id or f"rem_{uuid4().hex[:16]}",
        tenant_id=tenant_id,
        user_id=user_id,
        title="принять лекарство",
        trigger_at=trigger_at,
        next_trigger_at=next_trigger_at,
        recurrence_rule=recurrence_rule,
        status=status,
        bot_key="sreda",
    )
    session.add(row)
    return row


def _add_inbound_event(
    session: Session,
    *,
    tenant_id: str,
    status: str,
    created_at: datetime | None = None,
    classified_at: datetime | None = None,
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
    if created_at is not None:
        row.created_at = created_at
    if classified_at is not None:
        row.classified_at = classified_at
    session.add(row)
    return row


def _strip_tz(value: datetime | None) -> datetime | None:
    """SQLite DateTime(timezone=True) strips tzinfo on store; compare naive."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def _as_utc(value: datetime | None) -> datetime | None:
    """Re-attach UTC tzinfo to a naive datetime read back from SQLite."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# ---------------------------------------------------------------------------
# compute_next_occurrence_after — extracted shared helper
# ---------------------------------------------------------------------------


def test_compute_next_occurrence_after_returns_next_strictly_after() -> None:
    """The helper returns the next occurrence STRICTLY after the reference
    (inc=False), normalised to UTC."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    rule = "FREQ=DAILY;BYHOUR=9"
    # reference exactly ON an occurrence → next must be the FOLLOWING day
    reference = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    nxt = compute_next_occurrence_after(rule, dtstart, reference)
    assert nxt is not None
    assert _strip_tz(nxt) == datetime(2026, 1, 6, 9, 0)


def test_compute_next_occurrence_after_handles_no_future() -> None:
    """A finite rule whose occurrences are exhausted returns None."""
    dtstart = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    rule = "FREQ=DAILY;COUNT=2"  # only Jan 1 and Jan 2
    reference = datetime(2026, 1, 10, 9, 0, tzinfo=timezone.utc)
    assert compute_next_occurrence_after(rule, dtstart, reference) is None


def test_compute_next_occurrence_after_matches_legacy_callsites(
    db_session: Session,
) -> None:
    """The two former inline call-sites (mark_fired / acknowledge) produce the
    SAME next_trigger_at the helper computes — extraction did not change
    behaviour."""
    tid = "tenant_helper"
    _seed_tenant(db_session, tenant_id=tid, chat_id="900", user_id="u_h")
    trigger = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    rule = "FREQ=WEEKLY;BYDAY=MO;BYHOUR=9"
    current = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)

    expected = compute_next_occurrence_after(rule, trigger, current)
    assert expected is not None

    svc = HousewifeReminderService(db_session)

    # mark_fired call-site
    rem_mf = _add_reminder(
        db_session, tenant_id=tid, user_id="u_h",
        recurrence_rule=rule, trigger_at=trigger, next_trigger_at=trigger,
    )
    db_session.commit()
    svc.mark_fired(rem_mf, now=current)
    db_session.commit()
    db_session.refresh(rem_mf)
    assert _strip_tz(rem_mf.next_trigger_at) == _strip_tz(expected)

    # acknowledge call-site
    rem_ack = _add_reminder(
        db_session, tenant_id=tid, user_id="u_h",
        recurrence_rule=rule, trigger_at=trigger, next_trigger_at=trigger,
    )
    db_session.commit()
    svc.acknowledge(rem_ack, now=current)
    db_session.commit()
    db_session.refresh(rem_ack)
    assert _strip_tz(rem_ack.next_trigger_at) == _strip_tz(expected)


# ---------------------------------------------------------------------------
# A2 — restore не воскрешает (window-bounded)
# ---------------------------------------------------------------------------


def test_restore_drains_deletion_window_no_resurrection(
    db_session: Session,
) -> None:
    """A2: after soft_delete → restore, everything armed INSIDE the deletion
    window ``[deleted_at, restored_at]`` is neutralised:
      * one-shot in-window → terminal (cancelled), will NOT fire
      * recurring in-window → next_trigger_at advanced past restored_at
      * non-terminal inbound_event in-window → skipped
    Running the worker AFTER restore delivers nothing stale.

    Artefacts are placed AFTER soft_delete with ``deleted_at`` pinned to a
    controlled past anchor, so their trigger / created_at fall strictly inside
    the window (not before it).
    """
    tid = "tenant_restore"
    _seed_tenant(db_session, tenant_id=tid, chat_id="700", user_id="u_r")
    db_session.commit()

    # delete, then widen the window: deleted_at := now-2h, restored_at ≈ now.
    assert soft_delete_tenant(db_session, tid) is True
    deleted_at = _now() - timedelta(hours=2)
    _force_deleted_at(db_session, tid, deleted_at)

    # in-window trigger time (between deleted_at and now)
    in_window = _now() - timedelta(hours=1)

    one_shot = _add_reminder(
        db_session, tenant_id=tid, user_id="u_r",
        recurrence_rule=None, trigger_at=in_window, next_trigger_at=in_window,
    )
    recurring = _add_reminder(
        db_session, tenant_id=tid, user_id="u_r",
        recurrence_rule="FREQ=DAILY;BYHOUR=9", trigger_at=in_window,
        next_trigger_at=in_window,
    )
    inb = _add_inbound_event(
        db_session, tenant_id=tid, status="classified",
        created_at=in_window, classified_at=in_window,
    )
    db_session.commit()

    assert restore_tenant(db_session, tid) is True

    db_session.refresh(one_shot)
    db_session.refresh(recurring)
    db_session.refresh(inb)

    # tenant active again
    assert is_tenant_active(db_session, tid) is True

    # one-shot → terminal, NOT pending, will not fire
    assert one_shot.status == "cancelled"
    assert one_shot.next_trigger_at is None

    # recurring → still pending but advanced strictly past restored_at
    assert recurring.status == "pending"
    assert recurring.next_trigger_at is not None
    assert _as_utc(recurring.next_trigger_at) > _now() - timedelta(seconds=5)

    # inbound event → skipped + reason
    assert inb.status == "skipped"
    assert inb.status_reason == "tenant_deleted"

    # worker after restore: nothing stale delivered
    from sreda.workers.housewife_reminder_worker import HousewifeReminderWorker

    worker = HousewifeReminderWorker(db_session)
    fired = asyncio.run(worker.process_pending(now=_now()))
    assert fired == 0
    out_count = (
        db_session.query(OutboxMessage)
        .filter(OutboxMessage.tenant_id == tid)
        .count()
    )
    assert out_count == 0


def test_restore_leaves_future_reminders_untouched(db_session: Session) -> None:
    """A2 (no over-reach, UPPER bound): a pending reminder due AFTER
    restored_at (outside the deletion window) is NOT drained — it should still
    fire normally later."""
    tid = "tenant_future"
    _seed_tenant(db_session, tenant_id=tid, chat_id="710", user_id="u_f")
    future = _now() + timedelta(days=1)

    future_one_shot = _add_reminder(
        db_session, tenant_id=tid, user_id="u_f",
        recurrence_rule=None, trigger_at=future, next_trigger_at=future,
    )
    db_session.commit()

    soft_delete_tenant(db_session, tid)
    restore_tenant(db_session, tid)

    db_session.refresh(future_one_shot)
    # untouched: still pending, still armed for the future
    assert future_one_shot.status == "pending"
    assert _strip_tz(future_one_shot.next_trigger_at) == _strip_tz(future)


def test_restore_leaves_pre_deletion_overdue_reminder_untouched(
    db_session: Session,
) -> None:
    """🔴 A2 (no over-reach, LOWER bound — R1 MAJOR fix): a one-shot reminder
    already overdue BEFORE the deletion (``next_trigger_at < deleted_at``) is
    NOT cancelled by restore — it is not a product of the deletion window.

    This is the regression that the lower window bound closes. Without
    ``next_trigger_at >= deleted_at`` the restore drain would wrongly cancel it.
    """
    tid = "tenant_pre"
    _seed_tenant(db_session, tenant_id=tid, chat_id="715", user_id="u_p")
    db_session.commit()

    # delete, pin deleted_at to now-2h
    assert soft_delete_tenant(db_session, tid) is True
    deleted_at = _now() - timedelta(hours=2)
    _force_deleted_at(db_session, tid, deleted_at)

    # reminder overdue BEFORE deletion: next_trigger_at = now-3h < deleted_at
    pre_trigger = _now() - timedelta(hours=3)
    pre = _add_reminder(
        db_session, tenant_id=tid, user_id="u_p",
        recurrence_rule=None, trigger_at=pre_trigger, next_trigger_at=pre_trigger,
    )
    db_session.commit()

    assert restore_tenant(db_session, tid) is True

    db_session.refresh(pre)
    # untouched: still pending, trigger unchanged (NOT cancelled)
    assert pre.status == "pending"
    assert _strip_tz(pre.next_trigger_at) == _strip_tz(pre_trigger)


def test_restore_drains_in_window_one_shot_but_not_pre_window(
    db_session: Session,
) -> None:
    """A2 boundary pair in one tenant: an in-window one-shot is drained
    (cancelled) while a pre-window one-shot in the SAME restore is left
    pending — the lower bound discriminates them."""
    tid = "tenant_pair"
    _seed_tenant(db_session, tenant_id=tid, chat_id="716", user_id="u_pair")
    db_session.commit()

    assert soft_delete_tenant(db_session, tid) is True
    deleted_at = _now() - timedelta(hours=2)
    _force_deleted_at(db_session, tid, deleted_at)

    pre_trigger = _now() - timedelta(hours=3)  # before window
    in_trigger = _now() - timedelta(hours=1)   # inside window

    pre = _add_reminder(
        db_session, tenant_id=tid, user_id="u_pair",
        recurrence_rule=None, trigger_at=pre_trigger, next_trigger_at=pre_trigger,
    )
    inw = _add_reminder(
        db_session, tenant_id=tid, user_id="u_pair",
        recurrence_rule=None, trigger_at=in_trigger, next_trigger_at=in_trigger,
    )
    db_session.commit()

    assert restore_tenant(db_session, tid) is True

    db_session.refresh(pre)
    db_session.refresh(inw)
    assert pre.status == "pending"  # pre-window untouched
    assert inw.status == "cancelled"  # in-window drained
    assert inw.next_trigger_at is None


def test_restore_recurring_skips_all_missed_occurrences_in_window(
    db_session: Session,
) -> None:
    """A2: a recurring reminder with MANY missed daily occurrences inside the
    window is advanced to the FIRST occurrence STRICTLY AFTER restored_at — not
    just bumped a single step. Verified against ``compute_next_occurrence_after``
    on the same rule/anchor."""
    tid = "tenant_multi"
    _seed_tenant(db_session, tenant_id=tid, chat_id="717", user_id="u_m")
    db_session.commit()

    assert soft_delete_tenant(db_session, tid) is True
    # wide window: deleted 10 days ago, daily rule → ~10 missed occurrences
    deleted_at = _now() - timedelta(days=10)
    _force_deleted_at(db_session, tid, deleted_at)

    # daily rule anchored before deletion; next_trigger sits inside the window
    anchor = _now() - timedelta(days=10)
    in_window = _now() - timedelta(days=9)
    rule = "FREQ=DAILY;BYHOUR=9"
    rec = _add_reminder(
        db_session, tenant_id=tid, user_id="u_m",
        recurrence_rule=rule, trigger_at=anchor, next_trigger_at=in_window,
    )
    db_session.commit()

    before_restore = _now()
    assert restore_tenant(db_session, tid) is True

    db_session.refresh(rec)
    assert rec.status == "pending"
    nxt = _as_utc(rec.next_trigger_at)
    assert nxt is not None
    # A2: сдвинут на ПЕРВОЕ будущее вхождение строго после restore — пропущены ВСЕ
    # просроченные за окно (не на один шаг от anchor). Bound-проверки вместо
    # exact-equality с recompute-от-before_restore (Codex R2 MINOR: иначе флейк на
    # пересечении 9:00-границы между before_restore и внутренним restored_at кода).
    assert nxt > before_restore, "next_trigger должен быть строго после restore"
    # nxt — валидное вхождение правила (первое строго после nxt-1s = само nxt):
    assert compute_next_occurrence_after(rule, anchor, nxt - timedelta(seconds=1)) == nxt
    # и оно БЛИЖАЙШЕЕ будущее (DAILY → в пределах суток), не далёкое/одношаговое:
    assert nxt - before_restore <= timedelta(days=1, seconds=5)


def test_restore_recurring_exhausted_count_becomes_fired(
    db_session: Session,
) -> None:
    """A2: a recurring reminder whose finite COUNT is exhausted (no occurrence
    after restored_at) becomes terminal ``fired`` + ``next_trigger_at=None`` —
    mirroring the worker's behaviour when compute_next_occurrence_after returns
    None."""
    tid = "tenant_exhausted"
    _seed_tenant(db_session, tenant_id=tid, chat_id="718", user_id="u_e")
    db_session.commit()

    assert soft_delete_tenant(db_session, tid) is True
    deleted_at = _now() - timedelta(days=10)
    _force_deleted_at(db_session, tid, deleted_at)

    # COUNT=2 starting 10 days ago → both occurrences are long past restore.
    anchor = _now() - timedelta(days=10)
    in_window = _now() - timedelta(days=9)
    rule = "FREQ=DAILY;COUNT=2"
    rec = _add_reminder(
        db_session, tenant_id=tid, user_id="u_e",
        recurrence_rule=rule, trigger_at=anchor, next_trigger_at=in_window,
    )
    db_session.commit()

    # sanity: helper says no occurrence after now
    assert compute_next_occurrence_after(rule, anchor, _now()) is None

    assert restore_tenant(db_session, tid) is True

    db_session.refresh(rec)
    assert rec.status == "fired"
    assert rec.next_trigger_at is None


# ---------------------------------------------------------------------------
# A2 — inbound restore-drain (NON-VACUOUS — R1 MAJOR fix)
# ---------------------------------------------------------------------------


def test_restore_drains_inbound_inserted_after_delete_non_vacuous(
    db_session: Session,
) -> None:
    """🔴 A2 (non-vacuous inbound restore drain — R1 MAJOR fix): an inbound
    event inserted AFTER ``soft_delete_tenant`` (so it was NOT drained by the
    delete) with ``created_at`` inside the window is drained to ``skipped`` by
    ``restore_tenant`` ITSELF.

    Vacuity guard: the inbound drain is part of ``restore_tenant``; we prove the
    drain comes from the restore (not the delete) by neutralising the
    InboundEvent update inside ``restore_tenant`` via monkeypatch and asserting
    the event then stays non-terminal. With the patch the event is NOT skipped;
    without it (the real branch) it IS — so the assertion genuinely exercises
    the restore inbound branch.
    """
    tid = "tenant_inb"
    _seed_tenant(db_session, tenant_id=tid, chat_id="750", user_id="u_inb")
    db_session.commit()

    # 1) delete, pin window lower bound to now-2h
    assert soft_delete_tenant(db_session, tid) is True
    deleted_at = _now() - timedelta(hours=2)
    _force_deleted_at(db_session, tid, deleted_at)

    # 2) insert a NON-terminal inbound AFTER delete, created INSIDE the window.
    #    'new' status → classified_at IS NULL → must be caught by created_at.
    in_window = _now() - timedelta(hours=1)
    inb = _add_inbound_event(
        db_session, tenant_id=tid, status="new",
        created_at=in_window, classified_at=None,
    )
    db_session.commit()

    # precondition: still non-terminal right before restore (delete did NOT
    # touch it — it didn't exist yet) — proves the test is not vacuous.
    db_session.refresh(inb)
    assert inb.status == "new"

    # 3) restore → its inbound branch drains the event to skipped.
    assert restore_tenant(db_session, tid) is True
    db_session.refresh(inb)
    assert inb.status == "skipped"
    assert inb.status_reason == "tenant_deleted"


def test_restore_inbound_drain_neutralized_proves_branch(
    db_session: Session,
    monkeypatch,
) -> None:
    """Vacuity counter-proof: if the InboundEvent table is removed from the
    SQLAlchemy ``update`` dispatch path used by ``restore_tenant``, the same
    fixture is NOT skipped — confirming the previous test's pass is owed to the
    restore inbound branch, not to delete-drain or some other path.

    We monkeypatch ``InboundEvent.status`` reference indirectly by stubbing the
    module-level ``_INBOUND_NON_TERMINAL`` to an empty tuple, so the restore
    update's ``status.in_(())`` matches no rows. The event must remain ``new``.
    """
    import sreda.services.tenant_lifecycle as tl

    tid = "tenant_inb_neg"
    _seed_tenant(db_session, tenant_id=tid, chat_id="751", user_id="u_inbn")
    db_session.commit()

    assert soft_delete_tenant(db_session, tid) is True
    deleted_at = _now() - timedelta(hours=2)
    _force_deleted_at(db_session, tid, deleted_at)

    in_window = _now() - timedelta(hours=1)
    inb = _add_inbound_event(
        db_session, tenant_id=tid, status="new",
        created_at=in_window, classified_at=None,
    )
    db_session.commit()

    # Neutralise the restore inbound branch: empty non-terminal set → matches
    # nothing. (delete-drain already ran before this row existed.)
    monkeypatch.setattr(tl, "_INBOUND_NON_TERMINAL", ())

    assert restore_tenant(db_session, tid) is True
    db_session.refresh(inb)
    # NOT skipped — the restore inbound branch was the only thing that would
    # have skipped it; with it neutralised the row stays non-terminal.
    assert inb.status == "new"
    assert inb.status_reason is None


def test_restore_leaves_pre_window_inbound_untouched(
    db_session: Session,
) -> None:
    """🔴 A2 (no over-reach, inbound LOWER bound): an inbound event created and
    classified BEFORE ``deleted_at`` is NOT drained by restore (it predates the
    deletion window)."""
    tid = "tenant_inb_pre"
    _seed_tenant(db_session, tenant_id=tid, chat_id="752", user_id="u_ip")
    db_session.commit()

    assert soft_delete_tenant(db_session, tid) is True
    deleted_at = _now() - timedelta(hours=2)
    _force_deleted_at(db_session, tid, deleted_at)

    # created + classified 3h ago → both < deleted_at (before window)
    pre = _now() - timedelta(hours=3)
    inb = _add_inbound_event(
        db_session, tenant_id=tid, status="classified",
        created_at=pre, classified_at=pre,
    )
    db_session.commit()

    assert restore_tenant(db_session, tid) is True
    db_session.refresh(inb)
    # untouched: still non-terminal
    assert inb.status == "classified"
    assert inb.status_reason is None


def test_restore_drains_inbound_classified_in_window_created_before(
    db_session: Session,
) -> None:
    """A2 inbound: an event CREATED before the window but CLASSIFIED inside it
    is drained — the classified_at range catches the in-window transition even
    though created_at predates ``deleted_at``."""
    tid = "tenant_inb_cls"
    _seed_tenant(db_session, tenant_id=tid, chat_id="753", user_id="u_ic")
    db_session.commit()

    assert soft_delete_tenant(db_session, tid) is True
    deleted_at = _now() - timedelta(hours=2)
    _force_deleted_at(db_session, tid, deleted_at)

    created_before = _now() - timedelta(hours=3)   # before window
    classified_in = _now() - timedelta(hours=1)    # inside window
    inb = _add_inbound_event(
        db_session, tenant_id=tid, status="classified",
        created_at=created_before, classified_at=classified_in,
    )
    db_session.commit()

    assert restore_tenant(db_session, tid) is True
    db_session.refresh(inb)
    assert inb.status == "skipped"
    assert inb.status_reason == "tenant_deleted"


# ---------------------------------------------------------------------------
# A8 — обратимость + идемпотентность
# ---------------------------------------------------------------------------


def test_restore_reversible_and_idempotent(db_session: Session) -> None:
    """A8: soft_delete → restore → tenant active again; a second restore on an
    active tenant is a no-op (returns False)."""
    tid = "tenant_rev"
    _seed_tenant(db_session, tenant_id=tid, chat_id="720", user_id="u_rev")
    db_session.commit()

    assert is_tenant_active(db_session, tid) is True
    soft_delete_tenant(db_session, tid)
    assert is_tenant_active(db_session, tid) is False

    # restore brings it back
    assert restore_tenant(db_session, tid) is True
    assert is_tenant_active(db_session, tid) is True

    # second restore on an already-active tenant → no-op
    assert restore_tenant(db_session, tid) is False
    assert is_tenant_active(db_session, tid) is True


def test_restore_on_never_deleted_tenant_is_noop(db_session: Session) -> None:
    """A8 idempotency: restoring a tenant that was never deleted → no-op
    (returns False), does not raise."""
    tid = "tenant_active"
    _seed_tenant(db_session, tenant_id=tid, chat_id="730", user_id="u_a")
    db_session.commit()

    assert restore_tenant(db_session, tid) is False
    assert is_tenant_active(db_session, tid) is True


def test_restore_does_not_resurrect_drained_window(db_session: Session) -> None:
    """A8: drained/advanced rows are NOT resurrected. A repeated restore after a
    fresh delete cycle still leaves the window-drained one-shot terminal."""
    tid = "tenant_norez"
    _seed_tenant(db_session, tenant_id=tid, chat_id="740", user_id="u_n")
    db_session.commit()

    assert soft_delete_tenant(db_session, tid) is True
    deleted_at = _now() - timedelta(hours=2)
    _force_deleted_at(db_session, tid, deleted_at)

    in_window = _now() - timedelta(hours=1)
    one_shot = _add_reminder(
        db_session, tenant_id=tid, user_id="u_n",
        recurrence_rule=None, trigger_at=in_window, next_trigger_at=in_window,
    )
    db_session.commit()

    restore_tenant(db_session, tid)
    db_session.refresh(one_shot)
    assert one_shot.status == "cancelled"

    # a no-op second restore must not flip it back to pending
    restore_tenant(db_session, tid)
    db_session.refresh(one_shot)
    assert one_shot.status == "cancelled"
    assert one_shot.next_trigger_at is None
