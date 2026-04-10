import logging

from sqlalchemy import text

from sreda.db.session import get_engine

logger = logging.getLogger(__name__)


def database_is_ready() -> bool:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.warning("database health check failed", exc_info=True)
        return False
