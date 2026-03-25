from collections.abc import Generator

from sqlalchemy.orm import Session

from sreda.config.settings import Settings, get_settings
from sreda.db.session import get_db_session


def get_app_settings() -> Settings:
    return get_settings()


def get_session() -> Generator[Session, None, None]:
    yield from get_db_session()
