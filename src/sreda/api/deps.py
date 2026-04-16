import logging
from collections.abc import Generator
from functools import lru_cache

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from sreda.config.settings import Settings, get_settings
from sreda.db.session import get_db_session
from sreda.services.rate_limiter import InMemoryRateLimiter

logger = logging.getLogger(__name__)


def get_app_settings() -> Settings:
    return get_settings()


def get_session() -> Generator[Session, None, None]:
    yield from get_db_session()


# --- Rate limiting (H1) ------------------------------------------------
#
# Process-global limiters cached via ``lru_cache`` so each FastAPI worker
# has a single shared instance. These are defence-in-depth on top of any
# reverse-proxy limiter: they guarantee a floor of protection for
# single-process deployments and dev/debug runs where no ingress layer
# is in the picture.
#
# ``reset_rate_limiters`` exists so tests can drop cached singletons
# between cases — otherwise per-IP buckets leak across unrelated runs.


@lru_cache(maxsize=1)
def _connect_limiter() -> InMemoryRateLimiter:
    settings = get_settings()
    return InMemoryRateLimiter(
        max_requests=settings.rate_limit_connect_max_requests,
        window_seconds=settings.rate_limit_connect_window_seconds,
    )


@lru_cache(maxsize=1)
def _telegram_limiter() -> InMemoryRateLimiter:
    settings = get_settings()
    return InMemoryRateLimiter(
        max_requests=settings.rate_limit_telegram_max_requests,
        window_seconds=settings.rate_limit_telegram_window_seconds,
    )


@lru_cache(maxsize=1)
def _miniapp_limiter() -> InMemoryRateLimiter:
    settings = get_settings()
    return InMemoryRateLimiter(
        max_requests=settings.rate_limit_miniapp_max_requests,
        window_seconds=settings.rate_limit_miniapp_window_seconds,
    )


def reset_rate_limiters() -> None:
    _connect_limiter.cache_clear()
    _telegram_limiter.cache_clear()
    _miniapp_limiter.cache_clear()


def _client_ip(request: Request) -> str:
    # ``request.client`` is ``None`` for in-memory ASGI transports like
    # Starlette's TestClient. Fall back to a fixed key so the limiter
    # can still be exercised end-to-end in tests; in production the
    # host is always populated.
    if request.client is None:
        return "anonymous"
    return request.client.host or "anonymous"


def enforce_connect_rate_limit(request: Request) -> None:
    limiter = _connect_limiter()
    key = _client_ip(request)
    if not limiter.check(key):
        logger.warning("connect rate-limit hit for %s", key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limited",
        )


def enforce_telegram_rate_limit(request: Request) -> None:
    limiter = _telegram_limiter()
    key = _client_ip(request)
    if not limiter.check(key):
        logger.warning("telegram webhook rate-limit hit for %s", key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limited",
        )


def enforce_miniapp_rate_limit(request: Request) -> None:
    limiter = _miniapp_limiter()
    key = _client_ip(request)
    if not limiter.check(key):
        logger.warning("miniapp rate-limit hit for %s", key)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limited",
        )
