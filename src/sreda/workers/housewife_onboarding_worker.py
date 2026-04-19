"""Housewife onboarding kickoff worker.

When a user subscribes to ``housewife_assistant``, ``BillingService``
stamps ``kickoff_scheduled_at = now + 5 min`` into their onboarding
state. This worker polls each job-runner tick and, for users who:

  * still have ``status == not_started`` (the user didn't write first),
  * have a ``kickoff_scheduled_at`` in the past,
  * have a Telegram chat binding (so we can actually send),

…flips the flow to ``in_progress`` and enqueues an intro + first
question into the outbox. Delivery follows the normal OutboxDeliveryWorker
path so the message goes out within ~1s.

If the user writes first, the chat handler transitions the state to
``in_progress`` before this worker gets there — the ``status`` filter
below skips it naturally. No locking needed; SQLite + sequential
polling + idempotent service calls cover the race window.

Pattern intentionally mirrors ``housewife_reminder_worker``:
  ``async def process_pending(*, limit=50, now=None) -> int``
— so the same ``job_runner.process_pending_jobs_once`` gets it cheap.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models.core import OutboxMessage, User, Workspace
from sreda.db.models.user_profile import TenantUserSkillConfig
from sreda.db.repositories.user_profile import UserProfileRepository
from sreda.services.housewife_onboarding import (
    HOUSEWIFE_FEATURE_KEY,
    STATUS_NOT_STARTED,
    HousewifeOnboardingService,
)

logger = logging.getLogger(__name__)


_INTRO_MESSAGE = (
    "Привет! Я Среда — помощница по семейной рутине.\n\n"
    "Что я умею:\n"
    "• Напоминаю о делах — скажи «напомни в 18:00 забрать Машу»\n"
    "• Помню важное про семью — диеты, расписания, привычки\n"
    "• Ищу в интернете и читаю сайты — погода, расписания, факты\n"
    "• Понимаю голосовые сообщения\n\n"
    "Давай познакомимся — задам несколько коротких вопросов. "
    "Любой можно пропустить: скажи «потом» или «пропусти».\n\n"
    "Для начала — как мне к тебе обращаться?"
)


class HousewifeOnboardingKickoffWorker:
    """Fires the onboarding intro for users who subscribed ≥ ``delay_minutes``
    ago and haven't written first.

    The query scan is cheap at our scale (handful of users), so we
    materialise all housewife ``TenantUserSkillConfig`` rows and filter
    in Python — SQLite JSON operators are awkward and don't earn us
    anything here.
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.service = HousewifeOnboardingService(session)
        self.repo = UserProfileRepository(session)

    async def process_pending(
        self, *, limit: int = 50, now: datetime | None = None
    ) -> int:
        current = now or datetime.now(timezone.utc)
        rows = (
            self.session.query(TenantUserSkillConfig)
            .filter(TenantUserSkillConfig.feature_key == HOUSEWIFE_FEATURE_KEY)
            .limit(limit * 5)  # over-fetch; most are already in_progress
            .all()
        )
        fired = 0
        for row in rows:
            if fired >= limit:
                break
            params = UserProfileRepository.decode_skill_params(row)
            ob = params.get("onboarding") or {}
            if ob.get("status") != STATUS_NOT_STARTED:
                continue
            kickoff_iso = ob.get("kickoff_scheduled_at")
            if not kickoff_iso:
                continue
            try:
                kickoff_at = datetime.fromisoformat(kickoff_iso)
            except ValueError:
                logger.warning(
                    "housewife_onboarding: bad kickoff_scheduled_at=%r on tenant=%s user=%s",
                    kickoff_iso, row.tenant_id, row.user_id,
                )
                continue
            if kickoff_at.tzinfo is None:
                kickoff_at = kickoff_at.replace(tzinfo=timezone.utc)
            if kickoff_at > current:
                continue
            try:
                if self._fire(row.tenant_id, row.user_id):
                    fired += 1
            except Exception:  # noqa: BLE001 — one bad user must not kill the batch
                logger.exception(
                    "housewife_onboarding: kickoff failed tenant=%s user=%s",
                    row.tenant_id, row.user_id,
                )
                continue
        if fired:
            self.session.commit()
            logger.info("housewife onboarding: kicked off for %d user(s)", fired)
        return fired

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _fire(self, tenant_id: str, user_id: str) -> bool:
        """Send intro + flip status. Returns True if an outbox row was
        enqueued; False on soft failures (no chat binding, no workspace)
        so the caller can decide whether to count it."""
        chat_id = self._resolve_chat_id(user_id, tenant_id)
        if not chat_id:
            logger.warning(
                "housewife_onboarding: no telegram binding for tenant=%s user=%s, skipping",
                tenant_id, user_id,
            )
            return False
        workspace_id = self._resolve_workspace_id(tenant_id)
        if not workspace_id:
            logger.warning(
                "housewife_onboarding: no workspace for tenant=%s, skipping",
                tenant_id,
            )
            return False

        # Flip state first. If enqueue fails below we still want status
        # to reflect "we tried" — otherwise next tick re-fires and
        # duplicates the intro.
        self.service.start(tenant_id=tenant_id, user_id=user_id)

        payload = {
            "chat_id": chat_id,
            "text": _INTRO_MESSAGE,
            "reply_markup": None,
        }
        outbox = OutboxMessage(
            id=f"out_{uuid4().hex[:24]}",
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            channel_type="telegram",
            feature_key=HOUSEWIFE_FEATURE_KEY,
            status="pending",
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        if hasattr(OutboxMessage, "user_id"):
            outbox.user_id = user_id
        if hasattr(OutboxMessage, "is_interactive"):
            # Kickoff is a bot-initiated proactive message, NOT a reply
            # to a user command. Mark non-interactive so quiet-hours
            # policy applies normally.
            outbox.is_interactive = False
        self.session.add(outbox)
        self.session.flush()
        return True

    def _resolve_chat_id(self, user_id: str, tenant_id: str) -> str | None:
        user = self.session.get(User, user_id)
        if user and user.telegram_account_id:
            return user.telegram_account_id
        # Fallback: any user with a binding under the tenant.
        user = (
            self.session.query(User)
            .filter(
                User.tenant_id == tenant_id,
                User.telegram_account_id.is_not(None),
            )
            .order_by(User.id.asc())
            .first()
        )
        return user.telegram_account_id if user else None

    def _resolve_workspace_id(self, tenant_id: str) -> str | None:
        ws = (
            self.session.query(Workspace)
            .filter(Workspace.tenant_id == tenant_id)
            .order_by(Workspace.id.asc())
            .first()
        )
        return ws.id if ws else None
