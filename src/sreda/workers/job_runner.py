from __future__ import annotations

import asyncio

from sreda.config.settings import get_settings
from sreda.db.session import get_session_factory
from sreda.integrations.telegram.client import TelegramClient
from sreda.runtime.executor import ActionRuntimeService
from sreda.services.eds_account_verification import EDSAccountVerificationService


async def process_pending_jobs_once(*, limit: int = 20) -> int:
    settings = get_settings()
    session = get_session_factory()()
    try:
        telegram_client = (
            TelegramClient(settings.telegram_bot_token)
            if settings.telegram_bot_token
            else None
        )
        runtime_service = ActionRuntimeService(session, telegram_client=telegram_client)
        service = EDSAccountVerificationService(session, telegram_client=telegram_client)
        runtime_processed = await runtime_service.process_pending_jobs(limit=limit)
        verification_processed = await service.process_pending_jobs(limit=limit)
        return runtime_processed + verification_processed
    finally:
        session.close()


def run_job_loop() -> None:
    asyncio.run(process_pending_jobs_once())
