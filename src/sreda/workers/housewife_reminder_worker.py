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
from datetime import datetime, timedelta, timezone
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

# Аудит 2026-07-18 (#7): reminder без доставляемого канала (юзер отвязал
# TG/MAX) НЕ помечается fired, а откладывается на этот интервал. Час —
# не спамим тик каждые 5–10с, но и не теряем напоминание: канал появится
# (перепривязка) — доставим при ближайшем наступлении срока.
NO_CHANNEL_RETRY_MINUTES = 60


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
        deferred_no_channel = 0
        for reminder_id, tenant_id in due_ids:
            try:
                with tenant_session(tenant_id) as s:
                    # #344 F5 (Opus-адверсар, cross-process ack/snooze гонка):
                    # кнопки «Сделал ✅»/«Отложить ⏰» обрабатываются в ДРУГОМ
                    # процессе (uvicorn, telegram_bot._handle_reminder_callback), а
                    # этот воркер — в job_runner → второй конкурентный писатель в
                    # family_reminders уже при ОДНОМ воркере. Читали строку plain
                    # ``s.get`` без лока: fence видел pending, затем конкурентный
                    # ack/snooze коммитил новое состояние, а слепой ``mark_fired``
                    # ниже перетирал его (ack/snooze ТИХО ТЕРЯЛСЯ → повторный пинг /
                    # закрытие отложенного). Берём строку под ``SELECT ... FOR UPDATE``
                    # и держим лок до commit тика: конкурентный ack/snooze-UPDATE
                    # сериализуется — либо коммитится ДО (fence перечитает свежие
                    # status/next_trigger_at и пропустит), либо ПОСЛЕ нашего commit
                    # (его правка ложится поверх и выигрывает). PG-only (как due_now);
                    # SQLite (unit, 1 воркер) — без лока, поведение то же.
                    _bind = s.bind
                    _for_update = (
                        {"key_share": False}
                        if _bind is not None and _bind.dialect.name == "postgresql"
                        else None
                    )
                    # populate_existing: форсим свежий SELECT (с FOR UPDATE) даже
                    # если строка уже в identity-map — иначе get вернул бы кэш БЕЗ
                    # лока (Opus MINOR: latent identity-map fragility). Здесь сессия
                    # свежая per-row, но требование свежести делаем явным.
                    reminder = s.get(
                        FamilyReminder, reminder_id,
                        with_for_update=_for_update, populate_existing=True,
                    )
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

                    enqueued = self._enqueue_outbox_for(s, reminder)
                    if enqueued:
                        service.mark_fired(reminder, now=current)
                    else:
                        # Аудит 2026-07-18 (#7): нет доставляемого канала /
                        # workspace → mark_fired НЕ вызываем (one-shot
                        # закрывался бы без единой попытки доставки —
                        # молчаливая потеря пользовательского reminder'а).
                        # Откладываем: late-grace считается от
                        # next_trigger_at, поэтому silent-finalise ветка
                        # выше не сработает; канал появится — доставим.
                        reminder.next_trigger_at = current + timedelta(
                            minutes=NO_CHANNEL_RETRY_MINUTES
                        )
                        reminder.updated_at = current
                    s.commit()
                    if enqueued:
                        fired += 1
                    else:
                        deferred_no_channel += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "reminder %s: failed to fire, will retry next tick",
                    reminder_id,
                )
                continue
        if fired or skipped_late or deferred_no_channel:
            logger.info(
                "housewife: fired=%d skipped_late=%d deferred_no_channel=%d",
                fired, skipped_late, deferred_no_channel,
            )
        return fired

    # --- internals ------------------------------------------------------

    def _enqueue_outbox_for(self, session: Session, reminder: FamilyReminder) -> bool:
        """Поставить outbox-строки доставки по всем каналам reminder'а.

        Аудит 2026-07-18 (#7): возвращает True, если доставка ОБЕСПЕЧЕНА
        (хотя бы один routing получил новую строку или уже был поставлен
        другим писателем — дедуп по idem-key), False — если доставляемого
        канала / workspace нет (тогда вызывающий НЕ помечает fired, а
        откладывает reminder)."""
        routings = self._resolve_routings(session, reminder)
        if not routings:
            logger.warning(
                "reminder %s: tenant %s has no deliverable channel "
                "(нет ни TG, ни MAX account_id), skipping delivery",
                reminder.id,
                reminder.tenant_id,
            )
            return False

        workspace_id = self._resolve_workspace_id(session, reminder.tenant_id)
        if not workspace_id:
            logger.warning(
                "reminder %s: tenant %s has no workspace, skipping",
                reminder.id,
                reminder.tenant_id,
            )
            return False

        # Escalation UI: inline keyboard (кнопки «Сделал ✅»/«Отложить ⏰»).
        # ``callback_data`` — TG-style; MAX-доставка КОНВЕРТИРУЕТ их в свои inline-
        # attachments через ``render_max_inline_keyboard_attachment``
        # (``OutboxDeliveryWorker._send_now_max`` → ``integrations/max/client.py``),
        # так что MAX-юзер эти кнопки ВИДИТ и жмёт — обработчик
        # ``max_inbound._handle_max_reminder_callback`` (обе стороны ack/snooze
        # берут строку под FOR UPDATE, #344 F5). [Был устаревший коммент «MAX
        # text-only» — MAX давно рендерит кнопки; поправлено #344 F5.]
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
        # Доставка обеспечена (хотя бы один routing получил строку или
        # дедуп'нулся об уже поставленную) — можно mark_fired.
        return True

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
