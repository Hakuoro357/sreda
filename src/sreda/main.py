from fastapi import FastAPI

from sreda.api.routes.approvals import router as approvals_router
from sreda.api.routes.connect import router as connect_router
from sreda.api.routes.health import router as health_router
from sreda.api.routes.telegram_webhook import router as telegram_router
from sreda.config.logging import configure_logging
from sreda.config.settings import get_settings
from sreda.features.app_registry import get_feature_registry


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    feature_registry = get_feature_registry()

    app = FastAPI(title=settings.app_name)
    app.include_router(health_router)
    app.include_router(connect_router)
    app.include_router(telegram_router)
    app.include_router(approvals_router)
    feature_registry.register_api(app)
    app.state.feature_registry = feature_registry
    return app


app = create_app()
