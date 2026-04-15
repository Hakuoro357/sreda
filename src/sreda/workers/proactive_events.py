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

from sreda.db.models.core import OutboxMessage
from sreda.db.models.inbound_event import InboundEvent
from sreda.db.repositories.inbound_event import InboundEventRepository
from sreda.db.repositories.user_profile import UserProfileRepository
from sreda.features.app_registry import get_feature_registry
from sreda.runtime.handlers import RuntimeReply
from sreda.services.budget import BudgetService

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
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = InboundEventRepository(session)

    async def process_pending(
        self, *, limit: int = 50, min_score: float = 0.5
    ) -> int:
        events = self.repo.list_ready_for_delivery(limit=limit, min_score=min_score)
        processed = 0
        for event in events:
            try:
                await self._handle_event(event)
                processed += 1
            except Exception:  # noqa: BLE001
                logger.exception(
                    "proactive worker: handler failed for event %s", event.id
                )
                self.session.rollback()
                self.repo.mark_status(
                    event.id, status="skipped", reason="handler_exception"
                )
                self.session.commit()
        return processed

    async def _handle_event(self, event: InboundEvent) -> None:
        registry = get_feature_registry()
        handler = registry.get_proactive_handler(event.feature_key)
        if handler is None:
            self.repo.mark_status(
                event.id, status="skipped", reason="no_proactive_handler"
            )
            self.session.commit()
            return

        budget = BudgetService(self.session)
        if not budget.has_quota(event.tenant_id, event.feature_key):
            self.repo.mark_status(
                event.id, status="skipped", reason="quota_exhausted"
            )
            self.session.commit()
            return

        profile_dict: dict[str, Any] = {}
        memories: list[dict[str, Any]] = []
        if event.user_id:
            repo = UserProfileRepository(self.session)
            profile = repo.get_profile(event.tenant_id, event.user_id)
            if profile is not None:
                profile_dict = {
                    "display_name": profile.display_name,
                    "timezone": profile.timezone,
                    "quiet_hours": UserProfileRepository.decode_quiet_hours(profile),
                    "communication_style": profile.communication_style,
                    "interest_tags": UserProfileRepository.decode_interest_tags(profile),
                }

        context = ProactiveEventContext(
            session=self.session,
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

        chat_id = self._resolve_chat_id(event)
        if chat_id is None:
            self.repo.mark_status(
                event.id, status="skipped", reason="no_delivery_channel"
            )
            self.session.commit()
            return

        for reply in replies:
            self._write_outbox(event, reply, chat_id=chat_id)

        self.repo.mark_status(event.id, status="consumed")
        self.session.commit()

    def _resolve_chat_id(self, event: InboundEvent) -> str | None:
        """Find the Telegram chat_id for the event recipient.

        For now we walk ``User.telegram_account_id``; later this could
        be more flexible (per-user channel preference)."""
        if not event.user_id:
            return None
        from sreda.db.models.core import User

        user = self.session.get(User, event.user_id)
        if user is None or not user.telegram_account_id:
            return None
        return user.telegram_account_id

    def _write_outbox(
        self,
        event: InboundEvent,
        reply: RuntimeReply,
        *,
        chat_id: str,
    ) -> OutboxMessage:
        # Resolve workspace for the tenant. For proactive events we use
        # the user's workspace if known; otherwise first workspace of
        # the tenant. (Multi-workspace is edge case for current setup.)
        workspace_id = self._resolve_workspace_id(event)
        outbox = OutboxMessage(
            id=f"out_{uuid4().hex[:24]}",
            tenant_id=event.tenant_id,
            workspace_id=workspace_id,
            user_id=event.user_id,
            channel_type="telegram",
            feature_key=reply.feature_key or event.feature_key,
            is_interactive=False,  # proactive, not response-to-command
            status="pending",
            payload_json=json.dumps(
                {
                    "chat_id": chat_id,
                    "text": reply.text,
                    "reply_markup": reply.reply_markup,
                },
                ensure_ascii=False,
            ),
        )
        self.session.add(outbox)
        self.session.flush()
        return outbox

    def _resolve_workspace_id(self, event: InboundEvent) -> str:
        from sreda.db.models.core import Workspace

        ws = (
            self.session.query(Workspace)
            .filter(Workspace.tenant_id == event.tenant_id)
            .order_by(Workspace.id.asc())
            .first()
        )
        if ws is None:
            raise RuntimeError(
                f"tenant {event.tenant_id!r} has no workspace — can't route proactive reply"
            )
        return ws.id
