from __future__ import annotations

import asyncio

from sreda.config.settings import get_settings
from sreda.db.session import get_session_factory
from sreda.integrations.telegram.client import TelegramClient
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
        service = EDSAccountVerificationService(session, telegram_client=telegram_client)
        return await service.process_pending_jobs(limit=limit)
    finally:
        session.close()


def run_job_loop() -> None:
    asyncio.run(process_pending_jobs_once())
