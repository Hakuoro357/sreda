import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from sreda.admin.routes import router as admin_router
from sreda.api.routes.approvals import router as approvals_router
from sreda.api.routes.connect import router as connect_router
from sreda.api.routes.health import router as health_router
from sreda.api.routes.max_webhook import router as max_router
from sreda.api.routes.miniapp import router as miniapp_router
from sreda.api.routes.telegram_webhook import router as telegram_router
from sreda.config.logging import configure_logging
from sreda.config.settings import get_settings
from sreda.features.app_registry import get_feature_registry
from sreda.integrations.telegram.client import (
    close_pool as close_telegram_pool,
    run_keepalive_pinger,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """FastAPI lifespan — start/stop background tasks tied to uvicorn process.

    2026-04-29:
    * Telegram keepalive pinger — пингует `getMe` каждые 45с чтобы
      TCP+TLS connection в pool'е не остывал. Без него первый ack
      юзера после длинного простоя платит 800-900мс TLS-handshake
      через SOCKS5 egress.
    * On shutdown: gracefully cancel pinger + закрыть httpx pool
      (defensive — OS закроет FDs всё равно, но `aclose` чистит
      keep-alive sockets вовремя).
    """
    settings = get_settings()
    pinger_task: asyncio.Task | None = None
    if settings.telegram_bot_token:
        pinger_task = asyncio.create_task(
            run_keepalive_pinger(settings.telegram_bot_token),
            name="telegram-keepalive-pinger",
        )

    # Startup check: embeddings (2026-05-04 lesson). Best-effort, не блокирует
    # старт — шлёт alert в admin chat при проблеме.
    try:
        from sreda.services.embeddings import assert_embeddings_configured_or_alert
        await assert_embeddings_configured_or_alert()
    except Exception:  # noqa: BLE001
        logger.warning("embeddings startup-check raised", exc_info=True)

    # MAX webhook auto-register (2026-05-04, Phase 5 of MAX integration).
    # Fail-closed semantics (R1#3 from qwen review): app стартует, но
    # health endpoint показывает degraded если регистрация не удалась.
    # Rollback path: убрать SREDA_MAX_BOT_TOKEN из env → этот блок skip.
    app.state.max_webhook_status = "disabled"
    if settings.max_bot_token and settings.max_webhook_url:
        try:
            from sreda.integrations.max import MaxClient
            from sreda.services.admin_alerts import alert_admin_async

            max_client = MaxClient(token=settings.max_bot_token)
            await max_client.set_webhook(
                url=settings.max_webhook_url,
                secret_token=settings.max_webhook_secret_token,
            )
            app.state.max_webhook_status = "ok"
            logger.info(
                "MAX webhook registered: %s", settings.max_webhook_url,
            )
        except Exception as exc:  # noqa: BLE001
            logger.critical("MAX webhook registration failed: %s", exc)
            try:
                from sreda.services.admin_alerts import alert_admin_async
                await alert_admin_async(
                    f"🔴 startup: MAX webhook registration failed\n"
                    f"url: {settings.max_webhook_url}\n"
                    f"error: {exc}\n"
                    f"Бот глух в МАКС до тех пор пока не починим."
                )
            except Exception:  # noqa: BLE001
                logger.debug("admin alert failed (ignored)", exc_info=True)
            app.state.max_webhook_status = "failed"

    yield

    if pinger_task is not None and not pinger_task.done():
        pinger_task.cancel()
        try:
            await pinger_task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.debug("pinger shutdown: unexpected error", exc_info=True)
    try:
        await close_telegram_pool()
    except Exception:  # noqa: BLE001
        logger.debug("telegram pool close: unexpected error", exc_info=True)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        feature_requests_log_path=settings.feature_requests_log_path,
        trace_log_path=settings.trace_log_path,
    )
    feature_registry = get_feature_registry()

    app = FastAPI(title=settings.app_name, lifespan=_lifespan)
    app.include_router(admin_router)
    app.include_router(health_router)
    app.include_router(connect_router)
    app.include_router(miniapp_router)
    app.include_router(telegram_router)
    app.include_router(max_router)
    app.include_router(approvals_router)
    feature_registry.register_api(app)
    app.state.feature_registry = feature_registry
    return app


app = create_app()
