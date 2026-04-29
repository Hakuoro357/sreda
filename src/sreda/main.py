import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from sreda.admin.routes import router as admin_router
from sreda.api.routes.approvals import router as approvals_router
from sreda.api.routes.connect import router as connect_router
from sreda.api.routes.health import router as health_router
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
    app.include_router(approvals_router)
    feature_registry.register_api(app)
    app.state.feature_registry = feature_registry
    return app


app = create_app()
