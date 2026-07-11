"""Housewife reminders — background worker.

Polled by ``job_runner`` each tick. Finds reminders whose
``next_trigger_at`` has passed, composes outbox messages, advances the
reminder state (one-shot → fired, recurring → next occurrence).

Pattern follows ``workers/proactive_events.py::ProactiveEventWorker``:
one class per worker, ``async def process_pending(*, limit) -> int``.
The loop ordering in ``job_runner.process_pending_jobs_once`` ensures
this worker runs BEFORE ``OutboxDeliveryWorker`` so the reminders we
enqueue get delivered in the same tick.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreda.config.bot_registry import LEGACY_NULL_BOT_KEY
from sreda.db.models.core import OutboxMessage, User, Workspace
from sreda.db.models.housewife import FamilyReminder
from sreda.db.session import privileged_session, tenant_session
from sreda.services.housewife_reminders import (
    LATE_FIRE_GRACE_MINUTES,
    HousewifeReminderService,
)

logger = logging.getLogger(__name__)

HOUSEWIFE_FEATURE_KEY = "housewife_assistant"


class HousewifeReminderWorker:
    """Fires due ``FamilyReminder`` rows as outbox messages."""

    def __init__(self, *, registry=None) -> None:
        # #138 Ф2: воркер больше НЕ принимает общую сессию — сам ведёт свои
        # скоупы (privileged-скан + tenant_session на семью). job_runner его
        # больше не снабжает сессией.
        # #109: TelegramBotRegistry so resolve_outbox_routings can route a
        # reminder to the user's CURRENT bot (user.last_bot_key). Optional —
        # when None, resolve_outbox_routings leaves bot_key None and we fall
        # back to reminder.bot_key (pre-#109 behaviour). job_runner injects
        # the real registry; tests may omit it.
        self._registry = registry

    async def process_pending(
        self, *, limit: int = 50, now: datetime | None = None
    ) -> int:
        """Find reminders whose next_trigger_at is in the past, enqueue
        outbox messages, advance reminder state. Returns count fired.

        Called once per ``job_runner`` tick.

        #138 Ф2: изоляция семей. Скан всех due — КРОСС-ТЕНАНТНЫЙ →
        ``privileged_session`` (maintenance-роль видит все семьи). Отправка
        КОНКРЕТНОЙ семье — ПЕР-ТЕНАНТНАЯ → ``tenant_session(tenant_id)`` (RLS
        не даст записать чужой tenant_id). Каждая семья — СВОЯ транзакция
        (пер-итерационный коммит вместо одного на тик): сбой одной семьи не
        откатывает остальных; задвоения нет — держат idempotency_key доставки
        + fencing ``mark_fired``. ``now`` override для тестов."""
        current = now or datetime.now(timezone.utc)
        # Скан кросс-тенант → снимаем только id+tenant_id; ORM-строки detach'атся
        # при закрытии скан-сессии → ниже re-fetch под tenant_session семьи.
        with privileged_session("monitor") as scan:
            due_ids = [
                (r.id, r.tenant_id)
                for r in HousewifeReminderService(scan).due_now(
                    now=current, limit=limit
                )
            ]
        if not due_ids:
            return 0

        fired = 0
        skipped_late = 0
        for reminder_id, tenant_id in due_ids:
            try:
                with tenant_session(tenant_id) as s:
                    reminder = s.get(FamilyReminder, reminder_id)
                    if reminder is None:
                        continue  # исчез между сканом и действием
                    # #187 soft-delete — fencing-recheck (дверь #10): тенант мог
                    # быть удалён администратором мид-тик. Проверяем прямо перед
                    # доставкой + advance; удалён → пропуск (НЕ доставляем, НЕ
                    # двигаем state).
                    from sreda.services.tenant_lifecycle import is_tenant_active

                    if not is_tenant_active(s, tenant_id):
                        logger.info(
                            "reminder %s: tenant %s удалён (fencing) — пропуск без advance",
                            reminder_id, tenant_id,
                        )
                        continue
                    # #344 F5 fencing: reminder мог быть advance'нут другим воркером
                    # или прошлым тиком МЕЖДУ scan (due_now) и этим действием. due_now
                    # держал FOR UPDATE SKIP LOCKED только в scan-сессии — здесь строку
                    # не лочим, поэтому перепроверяем due/status по СВЕЖЕМУ состоянию:
                    #  - status != 'pending' (уже финализирован другим воркером) → skip;
                    #  - next_trigger_at в БУДУЩЕМ (advance'нут в след. эскалацию/итерацию)
                    #    → ещё не due → skip.
                    # Без этого late-grace видит future-триггер как «не просрочено» и
                    # фаерит преждевременно (двойной fire/advance). Двойную ДОСТАВКУ того
                    # же fire держит idempotency_key — здесь про fencing, не про delivery.
                    if reminder.status != "pending":
                        logger.info(
                            "reminder %s: status=%s (не pending) между scan и действием — "
                            "fencing skip", reminder_id, reminder.status,
                        )
                        continue
                    _ntt = reminder.next_trigger_at
                    if _ntt is not None:
                        if _ntt.tzinfo is None:
                            _ntt = _ntt.replace(tzinfo=timezone.utc)
                        if _ntt > current:
                            logger.info(
                                "reminder %s: next_trigger_at в будущем (advance'нут) — "
                                "fencing skip без fire", reminder_id,
                            )
                            continue
                    service = HousewifeReminderService(s)
                    # 2026-04-23 «баг 2b»: если напоминание просрочено больше
                    # чем LATE_FIRE_GRACE_MINUTES — закрываем silently без
                    # отправки. mark_fired всё равно зовём чтобы advance'нуть
                    # state (recurring → next RRULE, one-shot → status='fired').
                    trigger = reminder.next_trigger_at
                    if trigger is not None:
                        if trigger.tzinfo is None:
                            trigger = trigger.replace(tzinfo=timezone.utc)
                        late_min = (current - trigger).total_seconds() / 60
                        if late_min > LATE_FIRE_GRACE_MINUTES:
                            logger.info(
                                "reminder %s: past-due by %dmin > grace %dmin, "
                                "silent-finalise",
                                reminder_id, int(late_min),
                                LATE_FIRE_GRACE_MINUTES,
                            )
                            service.mark_fired(reminder, now=current)
                            s.commit()
                            skipped_late += 1
                            continue

                    self._enqueue_outbox_for(s, reminder)
                    service.mark_fired(reminder, now=current)
                    s.commit()
                    fired += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "reminder %s: failed to fire, will retry next tick",
                    reminder_id,
                )
                continue
        if fired or skipped_late:
            logger.info(
                "housewife: fired=%d skipped_late=%d",
                fired, skipped_late,
            )
        return fired

    # --- internals ------------------------------------------------------

    def _enqueue_outbox_for(self, session: Session, reminder: FamilyReminder) -> None:
        routings = self._resolve_routings(session, reminder)
        if not routings:
            logger.warning(
                "reminder %s: tenant %s has no deliverable channel "
                "(нет ни TG, ни MAX account_id), skipping delivery",
                reminder.id,
                reminder.tenant_id,
            )
            return

        workspace_id = self._resolve_workspace_id(session, reminder.tenant_id)
        if not workspace_id:
            logger.warning(
                "reminder %s: tenant %s has no workspace, skipping",
                reminder.id,
                reminder.tenant_id,
            )
            return

        # Escalation UI: inline keyboard. NB: callback_data format is
        # TG-style; для MAX inline-button format probe required (планируется
        # в follow-up). Сейчас MAX outbox row будет отправлена как text-only
        # т.к. ``OutboxDeliveryWorker._send_now_max`` не понимает TG
        # inline_keyboard schema.
        from sreda.services.ui_labels import BUTTON_ACK, BUTTON_SNOOZE

        text = f"🔔 {reminder.title}"
        reply_markup = {
            "inline_keyboard": [[
                {
                    "text": BUTTON_ACK,
                    "callback_data": f"rem_done:{reminder.id}",
                },
                {
                    "text": BUTTON_SNOOZE,
                    "callback_data": f"rem_snooze:{reminder.id}",
                },
            ]],
        }
        # Dual delivery (Boris directive 2026-05-05): создаём отдельную
        # outbox row на каждый available channel — юзер видит reminder
        # и в TG и в МАКСе.
        # #163 Фаза 4 — fired_trigger = триггер ИМЕННО ЭТОГО срабатывания (next_trigger_at ДО
        # mark_fired-advance). Дискриминирует эскалационные ре-пинги: у каждого свой next_trigger_at,
        # значит свой ключ → не схлопываются. None быть не должно (due_now фильтрует), но fallback на
        # trigger_at на всякий.
        fired = reminder.next_trigger_at or reminder.trigger_at
        if fired is not None and fired.tzinfo is None:
            fired = fired.replace(tzinfo=timezone.utc)
        fired_iso = fired.isoformat() if fired is not None else ""
        # Dual delivery (Boris directive 2026-05-05): создаём отдельную
        # outbox row на каждый available channel — юзер видит reminder
        # и в TG и в МАКСе.
        for routing in routings:
            row_bot_key = routing.bot_key or reminder.bot_key or LEGACY_NULL_BOT_KEY
            # #163 Фаза 4 — ключ идемпотентности доставки С КАНАЛОМ+ботом: dual TG+MAX различны
            # (разный канал), эскалации различны (разный fired_iso); повтор ТОЙ ЖЕ тройки
            # (мультипроцесс / повтор-enqueue) → дедуп. Pre-check (одно-поточный путь); partial-unique
            # индекс — backstop гонки.
            idem_key = f"{reminder.id}:{fired_iso}:{routing.channel}:{row_bot_key}"
            if self._outbox_key_exists(session, idem_key):
                logger.info(
                    "reminder %s: outbox idem-key уже поставлен (%s) — пропуск дубля доставки",
                    reminder.id, routing.channel,
                )
                continue
            payload = {
                "chat_id": routing.chat_id,
                "text": text,
                "reply_markup": reply_markup,
            }
            outbox = OutboxMessage(
                id=f"out_{uuid4().hex[:24]}",
                tenant_id=reminder.tenant_id,
                workspace_id=workspace_id,
                channel_type=routing.channel,
                feature_key=HOUSEWIFE_FEATURE_KEY,
                status="pending",
                payload_json=json.dumps(payload, ensure_ascii=False),
                # #109: deliver to the user's CURRENT bot (routing.bot_key,
                # populated from user.last_bot_key) when known; else fall
                # back to the reminder's frozen bot_key, then legacy default.
                bot_key=row_bot_key,
                idempotency_key=idem_key,
            )
            if hasattr(OutboxMessage, "user_id"):
                outbox.user_id = reminder.user_id
            if hasattr(OutboxMessage, "is_interactive"):
                outbox.is_interactive = False
            # SAVEPOINT (R1 MAJOR — субагент+Codex high+medium, мимо CRITICAL): гонку, которую
            # pre-check не закрыл, ловит partial-unique индекс на flush. БЕЗ savepoint IntegrityError
            # отравил бы ВСЮ тик-транзакцию (PendingRollbackError на финальном commit → откат всех
            # mark_fired-advance + доставка тика не идёт). begin_nested откатывает ТОЛЬКО эту строку
            # → гонка дедупится без падения тика.
            try:
                with session.begin_nested():
                    session.add(outbox)
                    session.flush()
            except IntegrityError:
                logger.info(
                    "reminder %s: outbox idem-key гонка (%s) → дедуп (другой писатель опередил)",
                    reminder.id, routing.channel,
                )
                continue

    def _outbox_key_exists(self, session: Session, idem_key: str) -> bool:
        """Pre-check дедупа доставки (#163 Фаза 4): есть ли уже outbox с этим ключом.
        Закрывает общий повтор-enqueue; гонку (TOCTOU) ловит partial-unique индекс + savepoint."""
        return (
            session.query(OutboxMessage.id)
            .filter(OutboxMessage.idempotency_key == idem_key)
            .first() is not None
        )

    def _resolve_routings(self, session: Session, reminder: FamilyReminder):
        """Reminder → list of OutboxRouting (10.6 dual-channel).

        Codex R1 CRITICAL fix: если ``reminder.user_id`` задан, мы НЕ
        должны fallback'аться на другого user'а tenant'а — это leak'ало
        бы личные reminder'ы (e.g. Боре приходило бы reminder сына, т.к.
        outbox.user_id остаётся сыном для quiet-hours / mute policy
        lookup). Fallback на random tenant-user'а только если
        ``reminder.user_id is None`` (tenant-wide reminder без owner'а).

        Codex R1 MAJOR #2: deliverable MAX = ``max_chat_id IS NOT NULL``
        (не ``max_account_id``), т.к. chat_id это recipient.
        """
        from sreda.services.channel_routing import resolve_outbox_routings
        from sreda.db.models.core import Tenant as _Tenant

        tenant = session.get(_Tenant, reminder.tenant_id)

        # User-scoped reminder: возвращаем routings ТОЛЬКО для своего
        # user'а. Если у юзера нет account'ов — empty list (skip + log),
        # НЕ fallback на чужого юзера.
        # Codex R2 MAJOR: проверяем что user.tenant_id == reminder.tenant_id
        # — defence against data inconsistency (manual SQL merge ошибся,
        # FK был snapshot'ом, и т.д.). Иначе personal-data leak в чужой
        # tenant.
        if reminder.user_id:
            user = session.get(User, reminder.user_id)
            if user is None or user.tenant_id != reminder.tenant_id:
                logger.warning(
                    "reminder %s: user %s mismatch tenant (user.tenant=%s "
                    "vs reminder.tenant=%s) — skipping (no leak)",
                    reminder.id, reminder.user_id,
                    user.tenant_id if user else None,
                    reminder.tenant_id,
                )
                return []
            return resolve_outbox_routings(
                session, tenant=tenant, user=user,
                telegram_bot_keys=self._registry,
            )

        # Tenant-wide reminder (user_id=None): берём любого юзера с
        # deliverable account.
        user = (
            session.query(User)
            .filter(
                User.tenant_id == reminder.tenant_id,
                (
                    User.telegram_account_id.is_not(None)
                    | User.max_chat_id.is_not(None)
                ),
            )
            .order_by(User.id.asc())
            .first()
        )
        if user is None:
            return []
        return resolve_outbox_routings(
            session, tenant=tenant, user=user,
            telegram_bot_keys=self._registry,
        )

    def _resolve_workspace_id(self, session: Session, tenant_id: str) -> str | None:
        ws = (
            session.query(Workspace)
            .filter(Workspace.tenant_id == tenant_id)
            .order_by(Workspace.id.asc())
            .first()
        )
        return ws.id if ws else None
