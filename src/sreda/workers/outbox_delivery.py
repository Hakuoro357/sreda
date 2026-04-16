"""Outbox delivery worker (Phase 2d).

Polls the ``outbox_messages`` queue and routes each pending row through
the per-user delivery policy:

  * ``send``  → Telegram send + status='sent'
  * ``defer`` → set ``scheduled_at`` to end-of-quiet-window, leave pending
  * ``drop``  → status='muted' (user set ``priority=mute`` for this skill)

Runs in the same polling loop as the skill-platform processor. The
cadence is defined by ``Settings.job_poll_interval_seconds``.

Note: interactive replies (replies to user commands) are already sent
inline by ``node_persist_replies`` — they arrive at the worker with
``status='sent'`` or ``'pending'`` (delivery retry). The worker just
retries those pending rows.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from sreda.db.models.core import OutboxMessage
from sreda.db.repositories.user_profile import UserProfileRepository
from sreda.features.app_registry import get_feature_registry
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.runtime.delivery_policy import DeliveryKind, decide_delivery

logger = logging.getLogger(__name__)


class OutboxDeliveryWorker:
    def __init__(
        self,
        session: Session,
        telegram_client: TelegramClient | None = None,
    ) -> None:
        self.session = session
        self.telegram = telegram_client

    async def process_pending_messages(
        self, *, now: datetime | None = None, limit: int = 50
    ) -> int:
        now_utc = now or datetime.now(timezone.utc)
        rows = (
            self.session.query(OutboxMessage)
            .filter(
                OutboxMessage.status == "pending",
                OutboxMessage.channel_type == "telegram",
                or_(
                    OutboxMessage.scheduled_at.is_(None),
                    OutboxMessage.scheduled_at <= now_utc,
                ),
            )
            .order_by(OutboxMessage.created_at.asc())
            .limit(limit)
            .all()
        )
        processed = 0
        for row in rows:
            await self._process_one(row, now_utc=now_utc)
            processed += 1
        return processed

    async def _process_one(self, row: OutboxMessage, *, now_utc: datetime) -> None:
        profile_dict, skill_config_dict = self._load_user_context(row)
        decision = decide_delivery(
            profile=profile_dict,
            skill_config=skill_config_dict,
            feature_key=row.feature_key,
            is_interactive=bool(row.is_interactive),
            now_utc=now_utc,
        )

        if decision.kind == DeliveryKind.drop:
            row.status = "muted"
            self.session.commit()
            return
        if decision.kind == DeliveryKind.defer:
            row.scheduled_at = decision.defer_until_utc
            # status stays 'pending'; worker will re-check after defer.
            self.session.commit()
            return

        # Send path
        await self._send_now(row)

    def _load_user_context(
        self, row: OutboxMessage
    ) -> tuple[dict | None, dict | None]:
        if not row.user_id:
            return None, None
        repo = UserProfileRepository(self.session)
        profile = repo.get_profile(row.tenant_id, row.user_id)
        profile_dict: dict | None = None
        if profile is not None:
            profile_dict = {
                "timezone": profile.timezone,
                "quiet_hours": UserProfileRepository.decode_quiet_hours(profile),
            }
        skill_config_dict: dict | None = None
        if row.feature_key:
            config = repo.get_skill_config(
                row.tenant_id, row.user_id, row.feature_key
            )
            if config is not None:
                skill_config_dict = {
                    "notification_priority": config.notification_priority,
                    "token_budget_daily": config.token_budget_daily,
                }
        return profile_dict, skill_config_dict

    async def _send_now(self, row: OutboxMessage) -> None:
        if self.telegram is None:
            # Dev/test path with no Telegram wired — just mark sent so
            # tests can assert policy without a client mock.
            row.status = "sent"
            self.session.commit()
            return
        try:
            payload = json.loads(row.payload_json or "{}")
        except json.JSONDecodeError:
            logger.exception("outbox delivery: bad payload_json for %s", row.id)
            row.status = "failed"
            self.session.commit()
            return
        try:
            await self.telegram.send_message(
                chat_id=payload.get("chat_id"),
                text=payload.get("text", ""),
                reply_markup=payload.get("reply_markup"),
                parse_mode=payload.get("parse_mode"),
            )
            # Feature-specific post-delivery (e.g. EDS photo sending)
            if row.feature_key:
                hook = get_feature_registry().get_delivery_hook(row.feature_key)
                if hook is not None:
                    try:
                        await hook(
                            session=self.session,
                            telegram_client=self.telegram,
                            outbox_row=row,
                            payload=payload,
                        )
                    except Exception:
                        logger.warning(
                            "outbox delivery: delivery hook failed for %s (feature=%s)",
                            row.id,
                            row.feature_key,
                            exc_info=True,
                        )
            row.status = "sent"
        except TelegramDeliveryError:
            logger.warning("outbox delivery: telegram error on %s, keeping pending", row.id)
            row.status = "pending"
        except Exception:
            logger.exception("outbox delivery: unexpected error on %s", row.id)
            row.status = "failed"
        self.session.commit()
