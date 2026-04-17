import logging
from logging.config import dictConfig


def configure_logging(level: str = "INFO") -> None:
    """Install a single stream handler with a timestamped format for
    the root logger AND for uvicorn's three named loggers.

    Why dictConfig instead of ``basicConfig``: uvicorn installs its own
    handlers on ``uvicorn``, ``uvicorn.error``, and ``uvicorn.access``
    at startup. ``basicConfig`` only touches root, so uvicorn's access
    log kept printing without a timestamp (``INFO:     192.168.x.x - "GET..."``).
    With ``dictConfig`` we replace uvicorn's handlers and set
    ``propagate=False`` so each line flows through our formatter
    exactly once — no duplicates, no stripped timestamps.
    """
    level_upper = level.upper()
    level_no = getattr(logging, level_upper, logging.INFO)
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                },
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "level": level_no,
                },
            },
            "root": {"level": level_no, "handlers": ["default"]},
            "loggers": {
                "uvicorn": {"level": level_no, "handlers": ["default"], "propagate": False},
                "uvicorn.error": {"level": level_no, "handlers": ["default"], "propagate": False},
                "uvicorn.access": {"level": level_no, "handlers": ["default"], "propagate": False},
            },
        }
    )
