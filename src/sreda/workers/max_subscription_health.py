"""Periodic MAX webhook subscription health check + token cleanup.

Phase 9 of MAX integration sprint. Запускается из ``run_job_loop_async``
в job_runner процессе, раз в 6 часов.

Что делает:
1. **Subscription health.** Если `max_bot_token` + `max_webhook_url`
   настроены — GET /subscriptions, проверить что наш URL в списке. Если
   нет — re-register + alert админу. Защищает от silent staleness когда
   MAX сбрасывает webhook (server restart, TTL, etc.).

2. **Token cleanup (R2#10 mandatory).** DELETE expired `channel_link_tokens`
   старше 1 дня от now() — не optional, иначе аккумулируются rows без
   ценности.

Best-effort: любая ошибка swallowed + log warning. Этот worker не должен
ломать job loop.
"""

from __future__ import annotations

import asyncio
import logging

from sreda.config.settings import get_settings
from sreda.db.session import get_session_factory


logger = logging.getLogger(__name__)


CHECK_INTERVAL_SECONDS = 6 * 60 * 60  # 6 часов


async def check_max_subscription_health() -> None:
    """One-shot subscription verification + cleanup."""
    settings = get_settings()

    # 1. Subscription health (only if MAX enabled).
    if settings.max_bot_token and settings.max_webhook_url:
        try:
            await _verify_max_subscription(settings)
        except Exception as exc:  # noqa: BLE001
            logger.warning("max sub health: verify failed: %s", exc)

    # 2. Token cleanup — независимо от MAX status (всегда работает,
    # token может быть от любой стороны).
    try:
        _cleanup_expired_tokens()
    except Exception as exc:  # noqa: BLE001
        logger.warning("max sub health: token cleanup failed: %s", exc)


async def _verify_max_subscription(settings) -> None:
    from sreda.integrations.max import MaxClient
    from sreda.services.admin_alerts import alert_admin_async

    if not settings.max_webhook_secret_token:
        logger.warning(
            "max sub health: secret_token не настроен — skip "
            "(URL содержит secret в path; без него register бессмысленный)"
        )
        return

    client = MaxClient(token=settings.max_bot_token)
    response = await client.get_subscriptions()
    subs = response.get("subscriptions", []) if isinstance(response, dict) else []
    # 2026-05-04 live-fix: secret в path, не в header. Final URL =
    # base + "/" + secret. Mask secret для логов.
    base_url = settings.max_webhook_url.rstrip("/")
    expected = f"{base_url}/{settings.max_webhook_secret_token}"
    expected_masked = f"{base_url}/<secret>"

    matches = [s for s in subs if isinstance(s, dict) and s.get("url") == expected]
    if matches:
        logger.info(
            "max sub health: OK (url=%s, %d subscriptions)",
            expected_masked, len(subs),
        )
        return

    # Stale / missing — re-register. Маскируем secret в любых URL'ах
    # которые мы логируем (включая foreign subs если в них вдруг наш token).
    masked_existing = [
        _mask_secret(s.get("url", ""), settings.max_webhook_secret_token)
        for s in subs if isinstance(s, dict)
    ]
    logger.warning(
        "max sub health: subscription stale (expected=%s, found=%s); "
        "re-registering",
        expected_masked, masked_existing,
    )
    try:
        await client.set_webhook(
            url=expected,
            secret_token=settings.max_webhook_secret_token,
        )
        await alert_admin_async(
            f"🟡 MAX subscription stale, re-registered\n"
            f"old_subs: {masked_existing}\n"
            f"new_url: {expected_masked}"
        )
    except Exception as exc:  # noqa: BLE001
        await alert_admin_async(
            f"🔴 MAX subscription stale AND re-register failed\n"
            f"url: {expected_masked}\n"
            f"error: {exc}"
        )


def _mask_secret(url: str, secret: str) -> str:
    """Replace secret token with literal <secret> in URL for safe logging."""
    if not secret:
        return url
    return url.replace(secret, "<secret>")


def _cleanup_expired_tokens() -> None:
    from sreda.services.channel_linking import cleanup_expired_tokens

    sf = get_session_factory()
    with sf() as session:
        deleted = cleanup_expired_tokens(session)
        if deleted:
            logger.info(
                "channel_link_tokens cleanup: deleted %d expired rows",
                deleted,
            )


async def run_health_check_loop() -> None:
    """Always-on loop: every 6h call ``check_max_subscription_health``.

    Spawned as background task в job_runner. Errors per-iteration
    swallowed (не убивают loop).
    """
    while True:
        try:
            await check_max_subscription_health()
        except Exception:  # noqa: BLE001
            logger.exception("max sub health: iteration failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
