"""#187 Phase 5 — A10 «Изоляция соседа»: soft-delete тенанта A НЕ затрагивает B.

Чеклист A10 (единственный пункт без теста до этой фазы):

    «Изоляция соседа. Given тенант A удалён, then тенант B работает штатно.
     Verified: test_neighbor_unaffected.»

RED-first (TDD). Все доказательства строятся ПАРНО: тот же артефакт, что у
удалённого тенанта A уходит в терминал/исключается, у живого соседа B остаётся
в исходном (рабочем) состоянии. Так тест НЕ вакуумен — если бы drain/due_now/
restore хватали по ошибке всех (без фильтра по ``tenant_id`` / ``deleted_at``),
B-ассерты упали бы.

Покрытие (B нетронут после ``soft_delete(A)``):
  1. ``is_tenant_active(B)`` остаётся True; ``B.deleted_at IS NULL``.
  2. drain (``soft_delete``) не задел B: B's pending outbox остаётся pending,
     B's message_jobs не форсированы в ``done``, B's нетерминальные inbound_events
     не ``skipped``, B's напоминания не тронуты. A's те же артефакты — терминальны.
  3. producer-фильтр ``due_now`` возвращает B's due-напоминание, НЕ A's (A
     исключён JOIN'ом ``Tenant.deleted_at IS NULL``).
  4. ``restore(A)`` не трогает артефакты / окно соседа B.
  5. аудит ``soft_delete(A, actor_type='admin')`` пишет строку ТОЛЬКО про A
     (``resource_id == A``); ни одной про B.

Имена полей / статусов / таблиц сверены по живым моделям и сервисам
(переиспользуем те же, что подтвердили Ф2a/Ф3):
- ``OutboxMessage``: ``status`` (default pending) / ``drop_reason`` String(64)
  (db/models/core.py). drain pending → ``dropped`` + ``drop_reason='tenant_deleted'``.
- ``MessageJob.status`` enum: pending/processing/done/failed/dead_letter
  (db/models/message_jobs.py); терминал требует ``finished_at IS NOT NULL``.
  drain pending/processing → ``done`` + ``finished_at`` + ``last_error``.
- ``InboundEvent.status``: new/needs_classification/classified/consumed/skipped;
  ``status_reason`` String(128) (db/models/inbound_event.py). drain
  нетерминальные → ``skipped`` + ``status_reason='tenant_deleted'``.
- ``FamilyReminder.status``: pending/fired/cancelled; ``recurrence_rule`` NULL =
  one-shot (db/models/housewife.py). drain напоминания НЕ трогает (R1 MAJOR).
- ``HousewifeReminderService.due_now`` (services/housewife_reminders.py:496):
  JOIN tenants … AND ``Tenant.deleted_at IS NULL`` — producer-фильтр.
- ``soft_delete_tenant`` / ``restore_tenant`` сигнатуры —
  services/tenant_lifecycle.py:258 / :433 (actor_type='admin' → audit-строка).
- ``AuditLog`` (db/models/audit.py:33): ``resource_type='tenant'`` /
  ``resource_id=<tenant_id>`` / ``action`` — пишется через
  ``audit_event`` (services/audit.py:44), actor_type ∈ {admin,user,system}.

Хелперы сидирования переиспользованы из ``test_187_phase2a_drain.py`` (импорт по
имени, не ``*`` — чтобы не тянуть ``test_*`` соседнего файла в этот модуль).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from sreda.db.models.audit import AuditLog
from sreda.db.models.core import Tenant
from sreda.services.housewife_reminders import HousewifeReminderService
from sreda.services.tenant_lifecycle import (
    is_tenant_active,
    restore_tenant,
    soft_delete_tenant,
)

# Переиспользуем сид-хелперы Ф2a (НЕ дублируем): подтверждённые поля моделей.
from tests.unit.test_187_phase2a_drain import (
    _add_inbound_event,
    _add_message_job,
    _add_outbox,
    _add_reminder,
    _seed_tenant,
)

A = "tenant_A_deleted"
B = "tenant_B_neighbor"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _seed_pair(db_session: Session) -> None:
    """Сидирует двух соседей A и B с РАЗНЫМИ chat_id/user_id."""
    _seed_tenant(db_session, tenant_id=A, chat_id="8001", user_id="u_A")
    _seed_tenant(db_session, tenant_id=B, chat_id="8002", user_id="u_B")


# ---------------------------------------------------------------------------
# 1. is_tenant_active(B) остаётся True; B.deleted_at IS NULL
# ---------------------------------------------------------------------------


def test_neighbor_stays_active_after_delete(db_session: Session) -> None:
    """A10: soft_delete(A) НЕ помечает B — is_tenant_active(B) True, флаг NULL."""
    _seed_pair(db_session)
    db_session.commit()

    assert is_tenant_active(db_session, A) is True
    assert is_tenant_active(db_session, B) is True

    assert soft_delete_tenant(db_session, A) is True

    # A помечен, B — нет.
    assert is_tenant_active(db_session, A) is False
    assert is_tenant_active(db_session, B) is True
    tenant_b = db_session.get(Tenant, B)
    assert tenant_b.deleted_at is None
    # И A действительно помечен (non-vacuous: флаг сработал, но только на A).
    tenant_a = db_session.get(Tenant, A)
    assert tenant_a.deleted_at is not None


# ---------------------------------------------------------------------------
# 2. drain (soft_delete) не задел B — парно с A
# ---------------------------------------------------------------------------


def test_neighbor_drain_untouched(db_session: Session) -> None:
    """A10 (drain-изоляция): A's pending-артефакты дренируются в терминал, B's —
    остаются в исходных рабочих статусах.

    Non-vacuous: сидируем ОБА тенанта одинаковыми pending-артефактами. Если бы
    drain не фильтровал по ``tenant_id``, B-ассерты упали бы (его строки тоже
    стали бы терминальными)."""
    _seed_pair(db_session)
    past = _now() - timedelta(minutes=1)

    # --- A: pending-артефакты (должны быть дренированы) ---
    a_outbox = _add_outbox(db_session, tenant_id=A, workspace_id=f"ws_{A}")
    a_job = _add_message_job(db_session, tenant_id=A, status="pending")
    a_job_proc = _add_message_job(db_session, tenant_id=A, status="processing")
    a_inb = _add_inbound_event(db_session, tenant_id=A, status="new")
    a_reminder = _add_reminder(
        db_session, tenant_id=A, user_id="u_A",
        recurrence_rule=None, next_trigger_at=past,
    )

    # --- B: ТЕ ЖЕ pending-артефакты (НЕ должны быть тронуты) ---
    b_outbox = _add_outbox(db_session, tenant_id=B, workspace_id=f"ws_{B}")
    b_job = _add_message_job(db_session, tenant_id=B, status="pending")
    b_job_proc = _add_message_job(db_session, tenant_id=B, status="processing")
    b_inb_new = _add_inbound_event(db_session, tenant_id=B, status="new")
    b_inb_needs = _add_inbound_event(
        db_session, tenant_id=B, status="needs_classification"
    )
    b_inb_classified = _add_inbound_event(
        db_session, tenant_id=B, status="classified"
    )
    b_reminder = _add_reminder(
        db_session, tenant_id=B, user_id="u_B",
        recurrence_rule=None, next_trigger_at=past,
    )
    b_recurring = _add_reminder(
        db_session, tenant_id=B, user_id="u_B",
        recurrence_rule="FREQ=DAILY;BYHOUR=9", next_trigger_at=past,
    )
    db_session.commit()

    assert soft_delete_tenant(db_session, A) is True

    for row in (
        a_outbox, a_job, a_job_proc, a_inb,
        b_outbox, b_job, b_job_proc,
        b_inb_new, b_inb_needs, b_inb_classified,
        b_reminder, b_recurring, a_reminder,
    ):
        db_session.refresh(row)

    # --- A's артефакты дренированы (non-vacuous: drain реально отработал) ---
    assert a_outbox.status == "dropped"
    assert a_outbox.drop_reason == "tenant_deleted"
    assert a_job.status == "done"
    assert a_job.last_error == "tenant_deleted"
    assert a_job_proc.status == "done"
    assert a_inb.status == "skipped"
    assert a_inb.status_reason == "tenant_deleted"

    # --- B's outbox: НЕ тронут (pending, без причины) ---
    assert b_outbox.status == "pending"
    assert b_outbox.drop_reason is None

    # --- B's message_jobs: НЕ форсированы в done ---
    assert b_job.status == "pending"
    assert b_job.last_error != "tenant_deleted"
    assert b_job_proc.status == "processing"
    assert b_job_proc.last_error != "tenant_deleted"

    # --- B's inbound_events: НЕ skipped, в исходных статусах ---
    assert b_inb_new.status == "new"
    assert b_inb_new.status_reason is None
    assert b_inb_needs.status == "needs_classification"
    assert b_inb_classified.status == "classified"

    # --- B's напоминания: НЕ тронуты (drain напоминания не трогает у НИКОГО,
    #     но проверяем именно сохранность соседа) ---
    assert b_reminder.status == "pending"
    assert b_reminder.next_trigger_at is not None
    assert b_recurring.status == "pending"
    assert b_recurring.next_trigger_at is not None
    assert b_recurring.recurrence_rule == "FREQ=DAILY;BYHOUR=9"


# ---------------------------------------------------------------------------
# 3. due_now (producer-filter) возвращает B's, НЕ A's
# ---------------------------------------------------------------------------


def test_neighbor_due_now_returns_only_neighbor(db_session: Session) -> None:
    """A10 (producer-фильтр): после soft_delete(A) ``due_now`` отдаёт due-
    напоминание B, но НЕ A — A исключён JOIN'ом ``Tenant.deleted_at IS NULL``.

    Оба напоминания due (next_trigger в прошлом) и pending (drain напоминания
    не трогает). Non-vacuous: ДО удаления due_now вернул бы ОБА; после —
    только B."""
    _seed_pair(db_session)
    past = _now() - timedelta(minutes=1)

    a_reminder = _add_reminder(
        db_session, tenant_id=A, user_id="u_A",
        recurrence_rule=None, next_trigger_at=past, reminder_id="rem_A",
    )
    b_reminder = _add_reminder(
        db_session, tenant_id=B, user_id="u_B",
        recurrence_rule=None, next_trigger_at=past, reminder_id="rem_B",
    )
    db_session.commit()

    svc = HousewifeReminderService(db_session)

    # До удаления: due_now видит обоих (доказывает, что фильтр — это именно
    # deleted_at, а не что-то иное исключающее A).
    before = {r.id for r in svc.due_now(now=_now())}
    assert "rem_A" in before
    assert "rem_B" in before

    assert soft_delete_tenant(db_session, A) is True
    # drain напоминания не трогает — A's reminder остаётся pending+due.
    db_session.refresh(a_reminder)
    assert a_reminder.status == "pending"

    after = {r.id for r in svc.due_now(now=_now())}
    # B's due-напоминание возвращается; A's — исключено producer-фильтром.
    assert "rem_B" in after
    assert "rem_A" not in after
    # B's reminder сам по себе не тронут.
    db_session.refresh(b_reminder)
    assert b_reminder.status == "pending"
    assert b_reminder.next_trigger_at is not None


# ---------------------------------------------------------------------------
# 4. restore(A) не трогает B
# ---------------------------------------------------------------------------


def test_neighbor_untouched_by_restore(db_session: Session) -> None:
    """A10 (restore-изоляция): восстановление A не задевает артефакты / окно
    соседа B.

    Сидируем B's pending one-shot + recurring + нетерминальный inbound с
    триггером/временем в том же интервале, что и окно удаления A. restore(A)
    дренит ТОЛЬКО артефакты A (по ``tenant_id``); B-артефакты остаются как
    были. Non-vacuous: A's артефакт в окне реально дренируется."""
    _seed_pair(db_session)
    db_session.commit()

    # Удаляем A и расширяем окно: deleted_at := now-2h.
    assert soft_delete_tenant(db_session, A) is True
    deleted_at = _now() - timedelta(hours=2)
    tenant_a = db_session.get(Tenant, A)
    tenant_a.deleted_at = deleted_at
    db_session.commit()

    in_window = _now() - timedelta(hours=1)  # внутри [deleted_at, restored_at]

    # A's one-shot в окне — restore ДОЛЖЕН его дренировать (non-vacuous).
    a_one_shot = _add_reminder(
        db_session, tenant_id=A, user_id="u_A",
        recurrence_rule=None, next_trigger_at=in_window, reminder_id="rem_A_inw",
    )
    # B's артефакты с тем же in-window временем — НЕ должны быть тронуты.
    b_one_shot = _add_reminder(
        db_session, tenant_id=B, user_id="u_B",
        recurrence_rule=None, next_trigger_at=in_window, reminder_id="rem_B_os",
    )
    b_recurring = _add_reminder(
        db_session, tenant_id=B, user_id="u_B",
        recurrence_rule="FREQ=DAILY;BYHOUR=9", next_trigger_at=in_window,
        reminder_id="rem_B_rec",
    )
    b_inb = _add_inbound_event(db_session, tenant_id=B, status="new")
    # created_at у B's inbound — внутрь окна A (тот же интервал).
    b_inb.created_at = in_window
    db_session.commit()

    # Снимок точных триггеров B ДО restore(A) — сверим на ТОЧНОЕ равенство после
    # (Codex Ф5 MAJOR: `is not None` пропустил бы сдвиг next_trigger_at, если бы
    # restore случайно потерял tenant_id-фильтр в recurring/one-shot ветке).
    b_one_shot_trigger_before = b_one_shot.next_trigger_at
    b_recurring_trigger_before = b_recurring.next_trigger_at

    assert restore_tenant(db_session, A) is True

    db_session.refresh(a_one_shot)
    db_session.refresh(b_one_shot)
    db_session.refresh(b_recurring)
    db_session.refresh(b_inb)

    # A's one-shot в окне дренирован (non-vacuous — restore реально отработал).
    assert a_one_shot.status == "cancelled"
    assert a_one_shot.next_trigger_at is None

    # B's артефакты НЕ тронуты restore(A) — ТОЧНОЕ сохранение, не только «жив».
    assert b_one_shot.status == "pending"
    assert b_one_shot.next_trigger_at == b_one_shot_trigger_before  # не сдвинут
    assert b_recurring.status == "pending"
    assert b_recurring.next_trigger_at == b_recurring_trigger_before  # recurring B не продвинут
    assert b_inb.status == "new"
    assert b_inb.status_reason is None

    # B по-прежнему активен, A снова активен.
    assert is_tenant_active(db_session, B) is True
    assert is_tenant_active(db_session, A) is True


# ---------------------------------------------------------------------------
# 5. аудит soft_delete(A) ничего не пишет про B
# ---------------------------------------------------------------------------


def test_neighbor_not_in_audit(db_session: Session) -> None:
    """A10 (аудит-изоляция): admin-инициированный soft_delete(A) пишет ровно
    одну audit-строку про A (``resource_id == A``); ни одной про B."""
    _seed_pair(db_session)
    db_session.commit()

    assert (
        soft_delete_tenant(
            db_session, A,
            actor_type="admin", actor_id="hash_admin", source="admin",
            reason="test",
        )
        is True
    )

    rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "tenant")
        .all()
    )
    resource_ids = [r.resource_id for r in rows]
    # Есть ровно одна строка — и она про A (non-vacuous: аудит реально записан).
    assert resource_ids == [A]
    # Явно: ни одной строки про B.
    assert B not in resource_ids
