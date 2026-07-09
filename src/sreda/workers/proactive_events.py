"""Proactive event worker (Phase 4).

Polls ``inbound_events`` for classified rows past the relevance
threshold, hands each to the skill's registered proactive handler,
writes replies to the outbox. The delivery worker then applies
quiet-hours / priority / throttle policy and sends via Telegram.

Budget gate: before invoking the skill handler, we check the skill's
quota via ``BudgetService``. Exhausted → event is marked ``skipped``
with reason; user won't see anything until next billing period
(or they buy an extra pack — Phase 4.5 /buy_extra).

Handler signature (see ``FeatureRegistry.register_proactive_handler``):

    def my_skill_handler(context: ProactiveEventContext) -> list[RuntimeReply]:
        ...

``context`` carries everything the handler needs — session, the event
itself (decoded payload), user profile snapshot, recent memories.
Handlers are free to call the LLM (must budget-record their own
usage). Most skills compose deterministic text and skip LLM entirely.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.config.bot_registry import LEGACY_NULL_BOT_KEY
from sreda.db.models.core import OutboxMessage
from sreda.db.models.inbound_event import InboundEvent
from sreda.db.repositories.inbound_event import InboundEventRepository
from sreda.db.repositories.user_profile import UserProfileRepository
from sreda.db.session import privileged_session, tenant_session
from sreda.features.app_registry import get_feature_registry
from sreda.runtime.handlers import RuntimeReply
from sreda.runtime.proactive_policy import (
    ProactiveDecisionKind,
    decide_proactive,
)
from sreda.services.budget import BudgetService
from sreda.services.embeddings import (
    EmbeddingClient,
    get_embeddings_client,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ProactiveEventContext:
    """What a proactive handler sees for a single event."""

    session: Session
    event: InboundEvent
    event_payload: dict[str, Any]
    profile: dict[str, Any]
    memories: list[dict[str, Any]]
    budget: BudgetService


class ProactiveEventWorker:
    def __init__(
        self,
        *,
        embedding_client: EmbeddingClient | None = None,
        system_bot_key: str | None = None,
        registry=None,
    ) -> None:
        # #138 Ф2: воркер сам ведёт скоупы (privileged-скан событий +
        # tenant_session на событие); общую сессию/repo больше не держит.
        # Embedding client is optional — when absent, decide_proactive's
        # semantic duplicate detection degrades to substring equality.
        # Falls back to settings-based factory so production deployments
        # get real embeddings without extra wiring.
        self.embedding_client = embedding_client
        # Phase 5: bot_key for system-generated outbox rows (no reminder
        # origin). Sourced from registry.system_default_bot_key in job_runner.
        self._system_bot_key = system_bot_key or LEGACY_NULL_BOT_KEY
        # #109: TelegramBotRegistry so resolve_outbox_routings can route to the
        # user's CURRENT bot (user.last_bot_key). Optional — when None,
        # routing.bot_key stays None and we fall back to _system_bot_key.
        self._registry = registry

    async def process_pending(
        self, *, limit: int = 50, min_score: float = 0.5
    ) -> int:
        # #138 Ф2: скан всех classified events — КРОСС-ТЕНАНТНЫЙ → privileged.
        # Снимок (id, tenant_id); ORM-строки detach'атся при закрытии скан-сессии
        # → ниже re-fetch под tenant_session события.
        with privileged_session("monitor") as scan:
            event_ids = [
                (e.id, e.tenant_id)
                for e in InboundEventRepository(scan).list_ready_for_delivery(
                    limit=limit, min_score=min_score
                )
            ]
        processed = 0
        for event_id, tenant_id in event_ids:
            try:
                with tenant_session(tenant_id) as s:
                    event = s.get(InboundEvent, event_id)
                    if event is None:
                        continue  # исчезло между сканом и обработкой
                    await self._handle_event(s, event)
                processed += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "proactive worker: handler failed for event %s", event_id
                )
                # Провалившая tenant_session уже закрыта/откачена → помечаем
                # skipped в СВЕЖЕЙ пер-тенантной сессии.
                try:
                    with tenant_session(tenant_id) as s2:
                        InboundEventRepository(s2).mark_status(
                            event_id, status="skipped", reason="handler_exception"
                        )
                        s2.commit()
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "proactive worker: mark-skipped failed for event %s", event_id
                    )
        return processed

    async def _handle_event(self, session: Session, event: InboundEvent) -> None:
        event_repo = InboundEventRepository(session)
        registry = get_feature_registry()
        handler = registry.get_proactive_handler(event.feature_key)
        if handler is None:
            event_repo.mark_status(
                event.id, status="skipped", reason="no_proactive_handler"
            )
            session.commit()
            return

        budget = BudgetService(session)
        if not budget.has_quota(event.tenant_id, event.feature_key):
            event_repo.mark_status(
                event.id, status="skipped", reason="quota_exhausted"
            )
            session.commit()
            return

        profile_dict: dict[str, Any] = {}
        memories: list[dict[str, Any]] = []
        if event.user_id:
            repo = UserProfileRepository(session)
            profile = repo.get_profile(event.tenant_id, event.user_id)
            if profile is not None:
                profile_dict = {
                    "display_name": profile.display_name,
                    "timezone": profile.timezone,
                    "quiet_hours": UserProfileRepository.decode_quiet_hours(profile),
                    "communication_style": profile.communication_style,
                    "interest_tags": UserProfileRepository.decode_interest_tags(profile),
                    "proactive_throttle_minutes": profile.proactive_throttle_minutes,
                }

        context = ProactiveEventContext(
            session=session,
            event=event,
            event_payload=InboundEventRepository.decode_payload(event),
            profile=profile_dict,
            memories=memories,
            budget=budget,
        )

        replies = handler(context) or []
        # Normalize — handlers may return a single RuntimeReply for convenience.
        if isinstance(replies, RuntimeReply):
            replies = [replies]

        routings = self._resolve_routings(session, event)
        if not routings:
            event_repo.mark_status(
                event.id, status="skipped", reason="no_delivery_channel"
            )
            session.commit()
            return

        # Resolve embedding client once (for duplicate detection across
        # all replies this turn). 2026-05-04 (Кати-incident lesson):
        # allow_fake убран. Без настроенных embeddings → DisabledEmbeddingClient
        # → embed_query raises RuntimeError → существующий decide_proactive
        # обрабатывает ошибку gracefully. Startup-check шлёт alert если
        # endpoint не настроен.
        embedding_client = self.embedding_client or get_embeddings_client()
        now_utc = datetime.now(timezone.utc)

        for reply in replies:
            decision = decide_proactive(
                session=session,
                reply_text=reply.text,
                tenant_id=event.tenant_id,
                user_id=event.user_id,
                feature_key=event.feature_key,
                profile=profile_dict,
                embedding_client=embedding_client,
                now_utc=now_utc,
            )
            # Dual delivery (10.6 Boris directive): для каждого reply
            # создаём отдельные outbox rows на ВСЕ available channels.
            for routing in routings:
                self._write_outbox_with_decision(
                    session,
                    event=event,
                    reply=reply,
                    routing=routing,
                    decision_kind=decision.kind,
                    defer_until=decision.defer_until_utc,
                    drop_reason=decision.drop_reason,
                )

        event_repo.mark_status(event.id, status="consumed")
        session.commit()

    def _resolve_routings(self, session: Session, event: InboundEvent):
        """10.6 dual-channel: list of OutboxRouting для proactive event.

        Возвращает все доступные channels (TG+MAX если оба set'нуты).
        Empty list если ни TG ни MAX account нет.

        Codex R2 MAJOR: defensive cross-tenant check — user.tenant_id
        должен совпадать с event.tenant_id (защита от data inconsistency).
        """
        if not event.user_id:
            return []
        from sreda.db.models.core import Tenant as _Tenant, User
        from sreda.services.channel_routing import resolve_outbox_routings

        user = session.get(User, event.user_id)
        if user is None or user.tenant_id != event.tenant_id:
            return []
        tenant = session.get(_Tenant, event.tenant_id)
        return resolve_outbox_routings(
            session, tenant=tenant, user=user,
            telegram_bot_keys=self._registry,
        )

    def _write_outbox_with_decision(
        self,
        session: Session,
        *,
        event: InboundEvent,
        reply: RuntimeReply,
        routing,  # OutboxRouting
        decision_kind: ProactiveDecisionKind,
        defer_until: datetime | None,
        drop_reason: str | None,
    ) -> OutboxMessage:
        """Persist a proactive reply with the outcome of decide_proactive.

        All three outcomes (send/defer/drop) produce a row — the ``drop``
        case writes a ``status='dropped'`` row with ``drop_reason`` so
        ``/stats`` can explain the silence to the user."""
        workspace_id = self._resolve_workspace_id(session, event)

        if decision_kind == ProactiveDecisionKind.send:
            status = "pending"
            scheduled_at: datetime | None = None
            row_drop_reason: str | None = None
        elif decision_kind == ProactiveDecisionKind.defer:
            status = "pending"
            scheduled_at = defer_until
            row_drop_reason = None
        else:  # drop
            status = "dropped"
            scheduled_at = None
            row_drop_reason = drop_reason

        payload: dict[str, Any] = {
            "chat_id": routing.chat_id,
            "text": reply.text,
            "reply_markup": reply.reply_markup,
        }
        if reply.parse_mode:
            payload["parse_mode"] = reply.parse_mode
        if reply.extra_payload:
            payload.update(reply.extra_payload)

        outbox = OutboxMessage(
            id=f"out_{uuid4().hex[:24]}",
            tenant_id=event.tenant_id,
            workspace_id=workspace_id,
            user_id=event.user_id,
            channel_type=routing.channel,  # 10.6 dynamic
            feature_key=reply.feature_key or event.feature_key,
            is_interactive=False,
            status=status,
            scheduled_at=scheduled_at,
            drop_reason=row_drop_reason,
            payload_json=json.dumps(payload, ensure_ascii=False),
            # #109: deliver to the user's CURRENT bot (routing.bot_key from
            # user.last_bot_key) when known; else the system default.
            bot_key=routing.bot_key or self._system_bot_key,
        )
        session.add(outbox)
        session.flush()
        return outbox

    def _resolve_workspace_id(self, session: Session, event: InboundEvent) -> str:
        from sreda.db.models.core import Workspace

        ws = (
            session.query(Workspace)
            .filter(Workspace.tenant_id == event.tenant_id)
            .order_by(Workspace.id.asc())
            .first()
        )
        if ws is None:
            raise RuntimeError(
                f"tenant {event.tenant_id!r} has no workspace — can't route proactive reply"
            )
        return ws.id
