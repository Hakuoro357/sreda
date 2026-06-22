"""Housewife reminders — domain service (no LLM, pure DB logic).

Used both by LLM-tool closures (from chat) and by the background worker
that fires due reminders. Keep this module import-cheap — no LangChain,
no LLM clients.

Recall-broadcast (Phase 2, 2026-05-04): on schedule, optional embedding
of ``f"Напоминание: {title}"`` via injected ``embedding_client``.
Failure non-fatal (warning + NULL embedding; backfill подхватит позже).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from uuid import uuid4

from dateutil.rrule import rrulestr
from sqlalchemy.orm import Session

from sreda.db.models.housewife import FamilyReminder
from sreda.services.embeddings import EMBEDDING_MODEL_NAME

if TYPE_CHECKING:  # avoid runtime LLM imports here (per module docstring)
    from sreda.services.embeddings import EmbeddingClient

logger = logging.getLogger(__name__)


# Escalation policy.
#
# 2026-04-23: auto-escalation ВЫКЛЮЧЕНА (MAX_FIRES=1 → ровно одна
# отправка). Причина — юзеры жаловались на дубли: re-ping через 2 мин
# воспринимался как спам, а race-условие с нажатием «Сделал» иногда
# позволяло напоминанию прилететь повторно даже после подтверждения.
# Кнопка «Отложить ⏰» — это явный action юзера, она продолжает
# работать через ``snooze()``. Если захотим вернуть — меняем обратно
# на 2, больше ничего трогать не надо.
ESCALATION_INTERVAL_MINUTES = 2
ESCALATION_MAX_FIRES = 1  # single-fire mode; no auto re-ping
SNOOZE_DEFAULT_MINUTES = 10

# 2026-04-23 «баг 2»: если напоминание просрочено БОЛЬШЕ чем на эту
# величину, воркер закрывает его silently — без отправки. Нужно чтобы
# серия one-shot'ов с прошедшими часами (LLM создала 3 на 11/15/20,
# юзер попросил в 20:42) не прилетала пачкой «дублей».
LATE_FIRE_GRACE_MINUTES = 15


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(value: datetime) -> datetime:
    """Normalise any datetime to UTC.

    Rules:
      * naive → assume UTC (tenant contract: all DB values are UTC).
      * aware non-UTC → convert to UTC (``astimezone``).
      * aware UTC → no-op.

    Why ``astimezone`` matters: SQLAlchemy + SQLite w/ DateTime(timezone=True)
    strips tzinfo on store. If we hand it an aware MSK datetime
    ``13:30+03:00`` without converting, SQLite stores the local portion
    ``13:30`` — which the worker later compares against ``now(UTC)``
    and (correctly) considers in the future. Result: reminders never
    fire. Always convert FIRST so stored string is genuinely UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _initial_next_trigger_at(trigger_at: datetime, recurrence_rule: str | None) -> datetime:
    """Return the first pending fire time for a newly scheduled reminder."""
    trigger_at = _coerce_utc(trigger_at)
    if not recurrence_rule:
        return trigger_at

    rule = rrulestr(recurrence_rule, dtstart=trigger_at)
    now = _coerce_utc(_utcnow())
    if trigger_at > now:
        return trigger_at

    next_occ = rule.after(now, inc=False)
    if next_occ is None:
        raise ValueError("recurrence_rule has no future occurrences")
    return _coerce_utc(next_occ)


@dataclass(slots=True)
class ReminderSummary:
    id: str
    title: str
    next_trigger_at: datetime | None
    recurrence_rule: str | None
    status: str


class HousewifeReminderService:
    """Create / list / cancel / fire reminders for a tenant.

    Writes commit after each mutation — no batch transactions — so tools
    can be invoked one-at-a-time from the chat LLM tool-loop without
    risking one bad call rolling back another.
    """

    def __init__(
        self,
        session: Session,
        *,
        embedding_client: "EmbeddingClient | None" = None,
    ) -> None:
        self.session = session
        # Optional. Когда задан — reminders получают embedding на schedule.
        # None в legacy callers — backfill подхватит позже.
        self._embedding_client = embedding_client

    def _embed_title(self, title: str) -> tuple[str | None, str | None]:
        """Embed reminder title with structural prefix.

        Short reminders («молоко», «брат др») имеют слабый semantic signal.
        Префикс ``Напоминание: ...`` даёт E5 контекст (R2#2 from qwen review).

        Returns ``(json, model)`` или ``(None, None)`` при ошибке.
        Failure non-fatal — reminder сохраняется без embedding.
        """
        if self._embedding_client is None:
            return None, None
        text = f"Напоминание: {title}"
        try:
            vec = self._embedding_client.embed_document(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reminder.embed failed (non-fatal): %s", exc
            )
            return None, None
        return json.dumps(vec), EMBEDDING_MODEL_NAME

    # --- user-facing (invoked from chat tools) --------------------------

    def schedule(
        self,
        *,
        tenant_id: str,
        user_id: str | None,
        title: str,
        trigger_at: datetime,
        recurrence_rule: str | None = None,
        source_memo: str | None = None,
        bot_key: str | None = None,
    ) -> FamilyReminder:
        trigger_at = _coerce_utc(trigger_at)
        # Validate rrule upfront — silently accepting a bad RRULE would
        # create a reminder that never fires.
        if recurrence_rule:
            try:
                rrulestr(recurrence_rule, dtstart=trigger_at)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"invalid recurrence_rule: {exc}") from exc
        next_trigger_at = _initial_next_trigger_at(trigger_at, recurrence_rule)

        clean_title = title.strip()
        embedding_json, embedding_model = self._embed_title(clean_title)

        from sreda.config.bot_registry import LEGACY_NULL_BOT_KEY
        resolved_bot_key = bot_key if bot_key is not None else LEGACY_NULL_BOT_KEY

        # #162 Фаза 0 — within-turn идемпотентность создания (эталон
        # services/housewife_shopping.py::add_items). Ветвление по наличию ToolRuntimeContext:
        #   ctx is None  → ЛЕГАСИ-путь (plan-execute/чат сегодня) — байт-в-байт, БЕЗ дедупа.
        #   ctx is not None → ReAct-путь: operation_id + INSERT ON CONFLICT + semantic_key + reuse.
        # #163 scope (план + контракт #162 + «не трогаем легаси»): межходовой замок (semantic_key
        # + дедуп) — ТОЛЬКО на ReAct (ctx) пути; легаси остаётся прежним (без хеша, без дедупа).
        from sreda.runtime.planner.tool_runtime import current_tool_runtime

        ctx = current_tool_runtime()

        if ctx is None:
            reminder = FamilyReminder(
                id=f"rem_{uuid4().hex[:24]}",
                tenant_id=tenant_id,
                user_id=user_id,
                title=clean_title,
                trigger_at=trigger_at,
                next_trigger_at=next_trigger_at,
                recurrence_rule=recurrence_rule,
                status="pending",
                source_memo=source_memo,
                embedding_json=embedding_json,
                embedding_model=embedding_model,
                bot_key=resolved_bot_key,
            )
            self.session.add(reminder)
            self.session.commit()
            return reminder

        # --- ReAct-путь (within-turn idempotency) -------------------------
        # fail-closed user_id (чеклист #162 п.8): nullable user_id ослабляет
        # UNIQUE (tenant,user,operation_id) — ctx-путь требует непустой user_id.
        if not user_id:
            raise ValueError(
                "schedule ctx path: user_id обязателен (fail-closed) — "
                "пустой user_id ослабил бы UNIQUE-дедуп."
            )
        # tenant-guard: контекст не должен утечь через границу тенанта.
        if ctx.tenant_id and ctx.tenant_id != tenant_id:
            raise ValueError(
                "schedule ctx path: ctx.tenant_id="
                f"{ctx.tenant_id!r} != tenant_id={tenant_id!r} — "
                "ToolRuntimeContext leaked across a tenant boundary."
            )

        from sreda.services.operation_id import compute_operation_id_create
        from sreda.services.text_normalization import normalize_for_dedup

        # logical_key напоминания — С ВРЕМЕНЕМ (R2 CRITICAL): «лекарство в 9:00»
        # и «в 21:00» — РАЗНЫЕ записи; title-only ключ схлопнул бы их.
        sep = "\x1f"
        logical_key = sep.join(
            [
                normalize_for_dedup(clean_title),
                trigger_at.isoformat(),
                recurrence_rule or "",
            ]
        )
        op_id = compute_operation_id_create(
            plan_id=ctx.execution_id,
            step_id=ctx.step_id,
            action="create",
            entity_type="family_reminder",
            logical_key=logical_key,
        )

        # #163 Фаза 2b — semantic_key (межходовой замок) ТОЛЬКО на ReAct-пути (scope #163; легаси
        # не трогаем). Напоминание всегда с временем → hash всегда есть. reuse — pre-check + backstop.
        from sqlalchemy.exc import IntegrityError

        from sreda.services.idempotent_ops import find_existing_pending_semantic
        from sreda.services.operation_id import compute_normalized_title_hash

        # `or None`: вырожденное название (только пунктуация) → нормализация даёт "" → хеш "".
        # Пустой hash IS NOT NULL → попал бы в партиал-индекс и ложно схлопнул разные такие записи;
        # трактуем "" как «дедуп невозможен» (None, вне индекса) — единообразно с легаси-NULL (субагент MINOR).
        nhash = compute_normalized_title_hash(
            clean_title, entity_type="family_reminder", tenant_id=tenant_id,
            user_id=user_id or "",
            extra=sep.join([trigger_at.isoformat(), recurrence_rule or ""])) or None

        dialect_name = self.session.bind.dialect.name  # type: ignore[union-attr]
        if dialect_name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as _insert
        else:
            from sqlalchemy.dialects.sqlite import insert as _insert  # type: ignore[no-redef]

        stmt = (
            _insert(FamilyReminder)
            .values(
                id=f"rem_{uuid4().hex[:24]}",
                tenant_id=tenant_id,
                user_id=user_id,
                title=clean_title,
                trigger_at=trigger_at,
                next_trigger_at=next_trigger_at,
                recurrence_rule=recurrence_rule,
                status="pending",
                source_memo=source_memo,
                embedding_json=embedding_json,
                embedding_model=embedding_model,
                bot_key=resolved_bot_key,
                operation_id=op_id,
                normalized_title_hash=nhash,
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "user_id", "operation_id"]
            )
        )
        # pre-check межходового дубля (одно-поточный путь + тесты): тот же semantic_key, pending →
        # reuse без вставки. ON CONFLICT(op_id) покрывает within-turn повтор; backstop ниже — гонку.
        if nhash is not None:
            existing = find_existing_pending_semantic(
                self.session, FamilyReminder, tenant_id=tenant_id, user_id=user_id, nhash=nhash)
            if existing is not None:
                return existing
        try:
            self.session.execute(stmt)
            self.session.commit()
        except IntegrityError:
            # backstop гонки (иной op_id, тот же semantic_key — межходовой) ловит partial-unique индекс.
            self.session.rollback()
            existing = (find_existing_pending_semantic(
                self.session, FamilyReminder, tenant_id=tenant_id,
                user_id=user_id, nhash=nhash) if nhash is not None else None)
            if existing is not None:
                return existing
            raise

        # SELECT-after-conflict: вернуть СТАБИЛЬНУЮ строку (тот же id) и при
        # вставке, и при ON CONFLICT (повтор внутри хода) — иначе replay вернул бы
        # «пусто» и потерял id (Codex R3 MAJOR).
        actual = (
            self.session.query(FamilyReminder)
            .filter(
                FamilyReminder.tenant_id == tenant_id,
                FamilyReminder.user_id == user_id,
                FamilyReminder.operation_id == op_id,
            )
            .one_or_none()
        )
        if actual is None:
            # Не должно случаться: либо вставка прошла, либо был конфликт.
            raise RuntimeError(
                "schedule ctx path: строка не найдена после INSERT для "
                f"op_id={op_id!r} (tenant={tenant_id!r})"
            )
        return actual

    def update(
        self,
        *,
        tenant_id: str,
        reminder_id: str,
        title: str | None = None,
        trigger_at: datetime | None = None,
        recurrence_rule: str | None = None,
        clear_recurrence: bool = False,
        commit: bool = True,
    ) -> FamilyReminder | None:
        """Patch an existing reminder in-place — used вместо delete+create
        когда юзер уточняет существующее напоминание.

        Returns the updated row, or None if not found / wrong tenant.

        Семантика:
          * ``title``               — если задан, перезаписывает + re-embed.
          * ``trigger_at``          — если задан, перезаписывает + сбрасывает
                                      ``next_trigger_at`` и ``escalation_count``.
          * ``recurrence_rule``     — если задан, валидируется через rrulestr.
                                      Передавать ``""`` (или используй
                                      ``clear_recurrence=True``) чтобы убрать.
          * ``clear_recurrence``    — explicit-флаг сброса в NULL (т.к.
                                      None в kwarg означает «не трогай»).

        Что НЕ обновляется этой операцией: ``status``, ``acknowledged_at``,
        ``last_fired_at``. Cancel — отдельный метод. Mark-fired — worker.
        """
        rem = self.session.get(FamilyReminder, reminder_id)
        if rem is None or rem.tenant_id != tenant_id:
            return None

        # Status guard (HIGH из reviewer 2026-05-04): не позволяем patch'ить
        # fired/cancelled reminders — иначе row из терминального статуса
        # может вернуться в pending-like состояние (новый next_trigger_at
        # + escalation_count=0) при том что worker'у он невидим. LLM
        # получит None и LLM-tool отрапортует юзеру «not found».
        if rem.status != "pending":
            return None

        # Blank-title guard (MEDIUM из reviewer): None означает «не трогай»,
        # пустая/whitespace строка — программерская ошибка, не «no-op».
        if title is not None and not title.strip():
            raise ValueError("title must not be blank")

        # Validate rrule (если меняется) до любых мутаций. Берём dtstart из
        # обновлённого trigger_at если есть, иначе старого.
        rrule_value: str | None
        if clear_recurrence:
            rrule_value = None
        elif recurrence_rule is not None:
            new_dtstart = (
                _coerce_utc(trigger_at) if trigger_at is not None
                else rem.trigger_at
            )
            try:
                rrulestr(recurrence_rule, dtstart=new_dtstart)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"invalid recurrence_rule: {exc}") from exc
            rrule_value = recurrence_rule
        else:
            rrule_value = rem.recurrence_rule  # unchanged

        new_trigger_at: datetime | None = None
        new_next_trigger_at: datetime | None = None
        if trigger_at is not None:
            new_trigger_at = _coerce_utc(trigger_at)
            new_next_trigger_at = _initial_next_trigger_at(new_trigger_at, rrule_value)
        elif recurrence_rule is not None:
            new_next_trigger_at = _initial_next_trigger_at(rem.trigger_at, rrule_value)

        if title is not None:
            clean = title.strip()
            if clean and clean != rem.title:
                rem.title = clean
                emb_json, emb_model = self._embed_title(clean)
                rem.embedding_json = emb_json
                rem.embedding_model = emb_model

        if new_trigger_at is not None:
            rem.trigger_at = new_trigger_at
            rem.next_trigger_at = new_next_trigger_at
            rem.escalation_count = 0  # reset escalation cycle on time change
        elif new_next_trigger_at is not None:
            rem.next_trigger_at = new_next_trigger_at
            rem.escalation_count = 0

        if clear_recurrence or recurrence_rule is not None:
            rem.recurrence_rule = rrule_value

        rem.updated_at = _utcnow()
        if commit:  # #163 Фаза 3: на ctx-пути commit владеет durable-helper (claim+мутация атомарны)
            self.session.commit()
        else:
            self.session.flush()
        return rem

    def list_active(
        self, *, tenant_id: str, user_id: str | None = None
    ) -> list[FamilyReminder]:
        q = (
            self.session.query(FamilyReminder)
            .filter(
                FamilyReminder.tenant_id == tenant_id,
                FamilyReminder.status == "pending",
            )
            .order_by(FamilyReminder.next_trigger_at.asc().nullslast())
        )
        if user_id:
            q = q.filter(FamilyReminder.user_id == user_id)
        return q.all()

    def count_active(
        self, *, tenant_id: str, user_id: str | None = None
    ) -> int:
        """Cheap count for UI badges (Mini App home screen). Avoids
        materializing rows we don't need for the "3 active" subtitle."""
        q = self.session.query(FamilyReminder).filter(
            FamilyReminder.tenant_id == tenant_id,
            FamilyReminder.status == "pending",
        )
        if user_id:
            q = q.filter(FamilyReminder.user_id == user_id)
        return q.count()

    def cancel(self, *, tenant_id: str, reminder_id: str, commit: bool = True) -> bool:
        reminder = (
            self.session.query(FamilyReminder)
            .filter(
                FamilyReminder.id == reminder_id,
                FamilyReminder.tenant_id == tenant_id,
            )
            .one_or_none()
        )
        if reminder is None or reminder.status == "cancelled":
            return False
        reminder.status = "cancelled"
        reminder.next_trigger_at = None
        reminder.updated_at = _utcnow()
        if commit:  # #163 Фаза 3: commit=False когда зовётся как часть большей операции (tasks.update)
            self.session.commit()
        else:
            self.session.flush()
        return True

    # --- worker-facing --------------------------------------------------

    def due_now(self, *, now: datetime | None = None, limit: int = 100) -> list[FamilyReminder]:
        """Cross-tenant fetch of pending reminders whose ``next_trigger_at``
        has passed. Called by the worker; NEVER expose to chat tools —
        they must stay tenant-scoped."""
        current = _coerce_utc(now or _utcnow())
        q = (
            self.session.query(FamilyReminder)
            .filter(
                FamilyReminder.status == "pending",
                FamilyReminder.next_trigger_at.isnot(None),
                FamilyReminder.next_trigger_at <= current,
            )
            .order_by(FamilyReminder.next_trigger_at.asc())
            .limit(limit)
        )
        # #163 Фаза 4 — FOR UPDATE SKIP LOCKED (только PG): при будущей многопроцессности второй
        # воркер пропустит уже залоченные due-строки → нет двойного пика/двойной постановки. Лок
        # держится до commit тика (после mark_fired). SQLite не поддерживает — без лока (1 воркер).
        bind = self.session.bind
        if bind is not None and bind.dialect.name == "postgresql":
            q = q.with_for_update(skip_locked=True)
        return q.all()

    def mark_fired(
        self, reminder: FamilyReminder, *, now: datetime | None = None
    ) -> None:
        """Called by the worker each time the reminder is sent to the
        user. Implements the escalation state machine:

          * First send (escalation_count=0 → 1): re-ping scheduled
            ``ESCALATION_INTERVAL_MINUTES`` later; status stays
            ``pending``; recurrence NOT advanced yet.
          * Subsequent sends up to ``ESCALATION_MAX_FIRES``: same
            thing, counter keeps climbing.
          * At ``ESCALATION_MAX_FIRES``: finalise this firing cycle —
            one-shot → ``status='fired'``, ``next_trigger_at=None``;
            recurring → advance to the next RRULE occurrence and reset
            ``escalation_count``.

        Caller commits (worker does it after all fires in the batch).
        """
        current = _coerce_utc(now or _utcnow())
        reminder.last_fired_at = current
        reminder.updated_at = current
        reminder.escalation_count = (reminder.escalation_count or 0) + 1

        if reminder.escalation_count < ESCALATION_MAX_FIRES:
            # Schedule the next re-ping. Counter carries forward.
            reminder.next_trigger_at = _coerce_utc(
                current + timedelta(minutes=ESCALATION_INTERVAL_MINUTES)
            )
            return

        # Cap reached — close out this firing cycle.
        reminder.escalation_count = 0
        if not reminder.recurrence_rule:
            reminder.status = "fired"
            reminder.next_trigger_at = None
            return

        try:
            rule = rrulestr(
                reminder.recurrence_rule,
                dtstart=_coerce_utc(reminder.trigger_at),
            )
            next_occ = rule.after(current, inc=False)
        except Exception:  # noqa: BLE001
            logger.exception(
                "reminder %s: failed to compute next occurrence, marking fired",
                reminder.id,
            )
            next_occ = None

        if next_occ is None:
            reminder.status = "fired"
            reminder.next_trigger_at = None
        else:
            reminder.next_trigger_at = _coerce_utc(next_occ)

    def acknowledge(
        self, reminder: FamilyReminder, *, now: datetime | None = None
    ) -> None:
        """User tapped "Сделал ✅" — close this firing cycle immediately.

        One-shot → status='fired', next_trigger_at=None.
        Recurring → advance to next RRULE occurrence without waiting
        for the re-ping timer. Records ``acknowledged_at`` for audit
        (users can look in the admin panel and see "this fire was
        acknowledged vs not"). Escalation counter resets.
        """
        current = _coerce_utc(now or _utcnow())
        reminder.acknowledged_at = current
        reminder.updated_at = current
        reminder.escalation_count = 0

        if not reminder.recurrence_rule:
            reminder.status = "fired"
            reminder.next_trigger_at = None
            return

        try:
            rule = rrulestr(
                reminder.recurrence_rule,
                dtstart=_coerce_utc(reminder.trigger_at),
            )
            next_occ = rule.after(current, inc=False)
        except Exception:  # noqa: BLE001
            logger.exception(
                "reminder %s: failed to compute next occurrence on ack",
                reminder.id,
            )
            next_occ = None

        if next_occ is None:
            reminder.status = "fired"
            reminder.next_trigger_at = None
        else:
            reminder.next_trigger_at = _coerce_utc(next_occ)

    def snooze(
        self,
        reminder: FamilyReminder,
        *,
        minutes: int = SNOOZE_DEFAULT_MINUTES,
        now: datetime | None = None,
    ) -> None:
        """User tapped "Отложить ⏰" — push next_trigger_at forward by
        ``minutes``. Escalation counter resets so the snoozed firing
        starts fresh. ``acknowledged_at`` cleared — user didn't mark
        the reminder done.
        """
        current = _coerce_utc(now or _utcnow())
        reminder.next_trigger_at = _coerce_utc(
            current + timedelta(minutes=max(1, int(minutes or 1)))
        )
        reminder.escalation_count = 0
        reminder.acknowledged_at = None
        reminder.status = "pending"
        reminder.updated_at = current

    def as_summary(self, reminder: FamilyReminder) -> ReminderSummary:
        return ReminderSummary(
            id=reminder.id,
            title=reminder.title,
            next_trigger_at=reminder.next_trigger_at,
            recurrence_rule=reminder.recurrence_rule,
            status=reminder.status,
        )
