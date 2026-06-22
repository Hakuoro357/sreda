"""#187 soft-delete — рубильник жизненного цикла тенанта (Фаза 0: `is_tenant_active`).

Источник истины soft-delete: один флаг `tenants.deleted_at` + одна детерминированная
проверка `is_tenant_active`. Проверка вынесена ОТДЕЛЬНО, НЕ внутрь `EntitlementGate.check`
(тот зовётся из 8 мест, а `allowed` чтит лишь в 2 — голос/инструменты её пропустят; см.
plan `db-fix-tenant-deletion-plan.md`). Вызывается fail-closed во всех дверях отсечения.

Дальнейшие фазы добавят сюда `soft_delete_tenant` / `restore_tenant` (флаг + барьер + drain).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from sreda.db.models.core import OutboxMessage, Tenant
from sreda.db.models.inbound_event import InboundEvent
from sreda.db.models.message_jobs import MessageJob

logger = logging.getLogger(__name__)

# Единая причина-метка терминирования дренированных артефактов. Записывается в
# поля-причины (outbox.drop_reason / message_jobs.last_error /
# inbound_events.status_reason), которые читаются админкой/диагностикой.
_DRAIN_REASON = "tenant_deleted"

# Нетерминальные статусы inbound_events (см. db/models/inbound_event.py:79-89):
# new / needs_classification / classified — ещё «в работе»; consumed/skipped —
# уже терминальны и НЕ трогаются.
_INBOUND_NON_TERMINAL = ("new", "needs_classification", "classified")


def is_tenant_active(session: Session, tenant_id: str) -> bool:
    """True ⟺ тенант существует И НЕ помечен удалённым (`deleted_at IS NULL`).

    **Fail-closed:** нет строки тенанта → ``False`` (не исключение, не ``True``).
    Реализовано через ``EXISTS`` — не тащит строку, дёшево на горячем пути.
    """
    stmt = select(
        exists().where(Tenant.id == tenant_id, Tenant.deleted_at.is_(None))
    )
    return bool(session.execute(stmt).scalar())


def soft_delete_tenant(session: Session, tenant_id: str) -> bool:
    """Помечает тенанта удалённым (`deleted_at`) И активно дренит pending-артефакты.

    Возвращает ``True`` если тенант был помечен в этом вызове, ``False`` если
    он уже удалён (no-op — повторный drain не запускается).

    **Идемпотентность:** если ``tenant.deleted_at`` уже не NULL — НИЧЕГО не
    делаем (не пере-штампуем флаг, не дренируем повторно).

    **Drain (терминально, с причиной)** — точные поля/статусы сверены по моделям:
      - **outbox** (``status='pending'``) → ``status='dropped'`` +
        ``drop_reason='tenant_deleted'`` (валидное значение, читается в /stats).
      - **message_jobs** (``status IN ('pending','processing')``) → прямой UPDATE
        ``status='done', finished_at=now, last_error='tenant_deleted'``. НЕ
        ``mark_done`` (у него WHERE worker_id/attempt не матчит pending); прямой
        UPDATE проходит оба CHECK (enum + status/timestamps — ``done`` требует
        ``finished_at IS NOT NULL``).
      - **inbound_events** (new/needs_classification/classified) →
        ``status='skipped'`` + ``status_reason='tenant_deleted'``.
      - **family_reminders** — НЕ дренируем (ни one-shot, ни recurring; R1
        MAJOR анти-воскрешение). От доставки удалённого тенанта их защищает
        producer-фильтр ``due_now`` (JOIN tenants AND deleted_at IS NULL) +
        fencing-recheck воркера + Фаза 3 restore-drain. At-delete drain в
        ``fired`` создавал бы вектор воскрешения через ``snooze()`` (он
        безусловно возвращает ``status='pending'``).

    **Барьер (advisory-lock) — НЕ здесь** (Фаза 2b). Воркеры закрываются
    fencing-recheck в своих циклах (см. outbox_delivery / housewife_reminder_worker).
    Вызывающий коммитит транзакцию (здесь только flush мутаций в сессию).
    """
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        # Нет строки — нечего удалять; fail-closed-семантика на чтении это уже
        # трактует как «неактивен». Возвращаем False (не помечали).
        logger.warning("soft_delete_tenant: tenant %s не найден — no-op", tenant_id)
        return False

    if tenant.deleted_at is not None:
        # Уже удалён — идемпотентный no-op, drain не повторяем.
        return False

    now = datetime.now(timezone.utc)
    tenant.deleted_at = now

    # --- outbox: pending → dropped --------------------------------------
    session.execute(
        update(OutboxMessage)
        .where(
            OutboxMessage.tenant_id == tenant_id,
            OutboxMessage.status == "pending",
        )
        .values(status="dropped", drop_reason=_DRAIN_REASON)
    )

    # --- message_jobs: pending/processing → done (прямой UPDATE) ---------
    session.execute(
        update(MessageJob)
        .where(
            MessageJob.tenant_id == tenant_id,
            MessageJob.status.in_(("pending", "processing")),
        )
        .values(status="done", finished_at=now, last_error=_DRAIN_REASON)
    )

    # --- inbound_events: нетерминальные → skipped -----------------------
    session.execute(
        update(InboundEvent)
        .where(
            InboundEvent.tenant_id == tenant_id,
            InboundEvent.status.in_(_INBOUND_NON_TERMINAL),
        )
        .values(status="skipped", status_reason=_DRAIN_REASON)
    )

    # --- family_reminders: НЕ дренируем (R1 MAJOR — анти-воскрешение) ----
    # При удалении напоминания НЕ трогаем (ни one-shot, ни recurring). От
    # доставки удалённого тенанта их защищает producer-фильтр воркера
    # (``due_now`` JOIN tenants AND deleted_at IS NULL) + fencing-recheck +
    # Фаза 3 (restore-window-drain). At-delete drain был избыточен И создавал
    # вектор воскрешения: терминальный ``fired`` + ``next_trigger_at=NULL`` мог
    # быть «оживлён» обратно в pending через ``snooze()`` (он безусловно ставит
    # ``status='pending'``). Чеклист A6 reminders НЕ требует — только outbox /
    # message_jobs / inbound_events.

    session.flush()
    logger.info("soft_delete_tenant: tenant %s помечен удалённым + drain выполнен", tenant_id)
    return True
