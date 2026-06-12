from __future__ import annotations

import asyncio
import logging

from sreda.config.bot_registry import TelegramBotRegistry, telegram_client_for
from sreda.config.logging import configure_logging
from sreda.config.settings import get_settings
from sreda.db.session import get_session_factory
from sreda.features.app_registry import get_feature_registry
from sreda.integrations.max import MaxClient
from sreda.runtime.executor import ActionRuntimeService
from sreda.services.eds_account_verification import EDSAccountVerificationService
from sreda.workers.housewife_onboarding_worker import (
    HousewifeOnboardingKickoffWorker,
)
from sreda.workers.housewife_reminder_worker import HousewifeReminderWorker
from sreda.workers.message_dispatcher import process_pending as process_message_queue
from sreda.workers.onboarding_aha_worker import OnboardingAhaWorker
from sreda.workers.outbox_delivery import OutboxDeliveryWorker
from sreda.workers.proactive_events import ProactiveEventWorker
from sreda.workers.retention_worker import RetentionWorker
from sreda.workers.skill_platform_processor import SkillPlatformJobProcessor

logger = logging.getLogger(__name__)


async def process_pending_jobs_once(*, limit: int = 20) -> int:
    settings = get_settings()
    registry = get_feature_registry()
    session = get_session_factory()()
    try:
        # Phase 4b: build registry first; resolve clients through it so every
        # send goes via the correct bot (not the hardcoded global token).
        bot_registry = TelegramBotRegistry.from_settings(settings)
        telegram_client = (
            telegram_client_for(bot_registry.system_default_bot_key, bot_registry)
            if settings.telegram_bot_token
            else None
        )
        # MAX client (2026-05-05): без него OutboxDeliveryWorker._send_now_max
        # уходит в dev-stub path и помечает row 'sent' БЕЗ реальной
        # отправки — Boris вёлся как «MAX delivered» когда на самом деле
        # юзеру ничего не приходило. Если SREDA_MAX_BOT_TOKEN не задан,
        # MaxClient=None — outbox row для MAX останется 'sent'-fake'ом
        # (acceptable для dev/test envs).
        max_client = (
            MaxClient(settings.max_bot_token)
            if settings.max_bot_token
            else None
        )
        runtime_service = ActionRuntimeService(
            session,
            telegram_client=telegram_client,
            bot_registry=bot_registry,
        )
        verification = EDSAccountVerificationService(session, telegram_client=telegram_client)
        skill_platform = SkillPlatformJobProcessor(session, registry)
        _sys_bot_key = bot_registry.system_default_bot_key
        # #109: pass bot_registry so resolve_outbox_routings can route async
        # notifications to the user's CURRENT bot (user.last_bot_key).
        proactive = ProactiveEventWorker(
            session, system_bot_key=_sys_bot_key, registry=bot_registry,
        )
        housewife_reminders = HousewifeReminderWorker(
            session, registry=bot_registry,
        )
        housewife_onboarding = HousewifeOnboardingKickoffWorker(
            session, system_bot_key=_sys_bot_key, registry=bot_registry,
        )
        onboarding_aha = OnboardingAhaWorker(
            session, system_bot_key=_sys_bot_key, registry=bot_registry,
        )
        delivery = OutboxDeliveryWorker(
            session,
            telegram_client=telegram_client,
            max_client=max_client,
            registry=bot_registry,
        )
        # Retention worker — внутренне throttle'ит до 1 раза в 24 часа,
        # state в /tmp/sreda-retention-state.json. На каждом tick
        # просто проверяет «пора ли». 152-ФЗ Часть 2.
        retention = RetentionWorker(session)
        # #139: суточная сводка надёжности (этап 0 программы) — тот же
        # каденс-паттерн (state-файл, раз в сутки, откат после провала)
        from sreda.workers.reliability_report import ReliabilityReportWorker
        reliability = ReliabilityReportWorker(session)

        # Order matters: proactive & housewife workers fill outbox →
        # delivery drains it within the same tick. The message queue
        # consumer (Sub-A2 of Plan-Execute Epic) runs early in the
        # tick so its dispatched turns can fill outbox along with the
        # rest before delivery drains.
        #
        # Codex R1 MAJOR 2026-05-26 — bound queue work per tick to
        # min(5, limit) so a queue-enabled tenant with backlog can't
        # monopolize the whole job_runner tick and starve runtime
        # jobs / reminders / outbox delivery. 5 is enough for typical
        # webhook bursts; remaining backlog catches up over the next
        # tick (~5-10s in production).
        message_queue_processed = await process_message_queue(
            limit=min(5, limit)
        )
        runtime_processed = await runtime_service.process_pending_jobs(limit=limit)
        verification_processed = await verification.process_pending_jobs(limit=limit)
        skill_processed = await skill_platform.process_pending_jobs(limit=limit)
        proactive_processed = await proactive.process_pending(limit=limit)
        housewife_processed = await housewife_reminders.process_pending(limit=limit)
        onboarding_processed = await housewife_onboarding.process_pending(limit=limit)
        aha_processed = await onboarding_aha.process_pending(limit=limit)
        delivery_processed = await delivery.process_pending_messages(limit=limit)
        retention_processed = await retention.process_pending()
        reliability_processed = await reliability.process_pending()
        return (
            message_queue_processed
            + runtime_processed
            + verification_processed
            + skill_processed
            + proactive_processed
            + housewife_processed
            + onboarding_processed
            + aha_processed
            + delivery_processed
            + retention_processed
            + reliability_processed
        )
    finally:
        session.close()


async def run_job_loop_async() -> None:
    """Always-on polling loop (spec 36 Stage 2).

    Sleeps ``job_poll_interval_seconds`` between empty passes; when work is
    found, loops immediately until the queue drains. Designed to be run as
    a long-lived process alongside the API.

    2026-05-04 (Phase 9 of MAX integration sprint): runs параллельно
    с MAX subscription health check + channel-link token cleanup
    (every 6h). Errors per-iteration swallowed independently — health
    check failure не валит job loop, и наоборот.
    """
    settings = get_settings()
    interval = max(0.1, settings.job_poll_interval_seconds)

    # Spawn long-running periodic max health check task. Best-effort —
    # если max_bot_token не настроен, check сам skip'нется.
    from sreda.workers.max_subscription_health import run_health_check_loop
    health_task = asyncio.create_task(
        run_health_check_loop(), name="max_subscription_health",
    )

    # SVO «ковёр» monitor (2026-05-18, Boris ad-hoc request): poll
    # t.me/s/svo_online каждые 60s, alert админу при появлении точной
    # фразы об ограничениях в воздушном пространстве Шереметьево.
    from sreda.workers.svo_monitor import run_svo_monitor_loop
    svo_task = asyncio.create_task(
        run_svo_monitor_loop(), name="svo_monitor",
    )

    try:
        while True:
            try:
                processed = await process_pending_jobs_once()
            except Exception:  # noqa: BLE001 — we never want the loop to die
                logger.exception("job_runner: iteration failed, continuing")
                processed = 0
            if processed == 0:
                await asyncio.sleep(interval)
    finally:
        for task in (health_task, svo_task):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def run_job_loop() -> None:
    """Entry point for the worker process.

    Configures logging up-front so ``sreda.trace`` / ``sreda.feature_requests``
    etc. get their dedicated file handlers in this process too (the
    trace delivery event is emitted from the OutboxDeliveryWorker,
    which lives here, not in the uvicorn process). Without this call,
    we inherit Python's default root-logger config and dedicated log
    files stay empty.
    """
    settings = get_settings()
    configure_logging(
        settings.log_level,
        feature_requests_log_path=settings.feature_requests_log_path,
        trace_log_path=settings.trace_log_path,
    )
    if settings.job_poll_interval_seconds <= 0:
        # Legacy one-shot mode used by cron-driven deployments.
        asyncio.run(process_pending_jobs_once())
        return
    asyncio.run(run_job_loop_async())


if __name__ == "__main__":
    # ``python -m sreda.workers.job_runner`` entrypoint for production.
    run_job_loop()
