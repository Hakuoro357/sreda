from __future__ import annotations

import asyncio
import logging

from sreda.config.settings import get_settings
from sreda.db.session import get_session_factory
from sreda.features.app_registry import get_feature_registry
from sreda.integrations.telegram.client import TelegramClient
from sreda.runtime.executor import ActionRuntimeService
from sreda.services.eds_account_verification import EDSAccountVerificationService
from sreda.workers.housewife_reminder_worker import HousewifeReminderWorker
from sreda.workers.outbox_delivery import OutboxDeliveryWorker
from sreda.workers.proactive_events import ProactiveEventWorker
from sreda.workers.skill_platform_processor import SkillPlatformJobProcessor

logger = logging.getLogger(__name__)


async def process_pending_jobs_once(*, limit: int = 20) -> int:
    settings = get_settings()
    registry = get_feature_registry()
    session = get_session_factory()()
    try:
        telegram_client = (
            TelegramClient(settings.telegram_bot_token)
            if settings.telegram_bot_token
            else None
        )
        runtime_service = ActionRuntimeService(session, telegram_client=telegram_client)
        verification = EDSAccountVerificationService(session, telegram_client=telegram_client)
        skill_platform = SkillPlatformJobProcessor(session, registry)
        proactive = ProactiveEventWorker(session)
        housewife_reminders = HousewifeReminderWorker(session)
        delivery = OutboxDeliveryWorker(session, telegram_client=telegram_client)

        # Order matters: proactive & housewife workers fill outbox →
        # delivery drains it within the same tick.
        runtime_processed = await runtime_service.process_pending_jobs(limit=limit)
        verification_processed = await verification.process_pending_jobs(limit=limit)
        skill_processed = await skill_platform.process_pending_jobs(limit=limit)
        proactive_processed = await proactive.process_pending(limit=limit)
        housewife_processed = await housewife_reminders.process_pending(limit=limit)
        delivery_processed = await delivery.process_pending_messages(limit=limit)
        return (
            runtime_processed
            + verification_processed
            + skill_processed
            + proactive_processed
            + housewife_processed
            + delivery_processed
        )
    finally:
        session.close()


async def run_job_loop_async() -> None:
    """Always-on polling loop (spec 36 Stage 2).

    Sleeps ``job_poll_interval_seconds`` between empty passes; when work is
    found, loops immediately until the queue drains. Designed to be run as
    a long-lived process alongside the API."""
    settings = get_settings()
    interval = max(0.1, settings.job_poll_interval_seconds)
    while True:
        try:
            processed = await process_pending_jobs_once()
        except Exception:  # noqa: BLE001 — we never want the loop to die
            logger.exception("job_runner: iteration failed, continuing")
            processed = 0
        if processed == 0:
            await asyncio.sleep(interval)


def run_job_loop() -> None:
    """Entry point for the worker process."""
    settings = get_settings()
    if settings.job_poll_interval_seconds <= 0:
        # Legacy one-shot mode used by cron-driven deployments.
        asyncio.run(process_pending_jobs_once())
        return
    asyncio.run(run_job_loop_async())


if __name__ == "__main__":
    # ``python -m sreda.workers.job_runner`` entrypoint for production.
    run_job_loop()
