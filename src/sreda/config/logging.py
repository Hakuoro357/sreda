import logging
from logging.config import dictConfig


def _build_config(level_no: int) -> dict:
    return {
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
            # propagate=True here is the critical bit: uvicorn's access
            # formatter expects ``record.args`` to be a 5-tuple (method,
            # path, proto, status, size). When we emit *plain* log lines
            # through this logger (from tests, from our own code), those
            # args are empty and uvicorn's formatter crashes. Letting the
            # handler live on root via propagation means access lines
            # flow through our simple formatter regardless of args shape.
            "uvicorn.access": {"level": level_no, "handlers": [], "propagate": True},
        },
    }


def configure_logging(level: str = "INFO") -> None:
    """Install a timestamped stream handler for the root logger and for
    uvicorn's named loggers, and mutate ``uvicorn.config.LOGGING_CONFIG``
    in place so uvicorn's own ``Server.serve()`` — which re-applies
    ``LOGGING_CONFIG`` AFTER we configure the app — picks up our format.

    Without the in-place mutation, uvicorn's startup wipes out our
    dictConfig and the access log reverts to the default
    ``INFO:     1.2.3.4:5678 - "GET /..."`` (no timestamp).
    """
    level_upper = level.upper()
    level_no = getattr(logging, level_upper, logging.INFO)
    cfg = _build_config(level_no)
    dictConfig(cfg)

    # Mutate uvicorn's module-level config so its `Config.configure_logging`
    # (called after app import) applies OUR config instead of its defaults.
    try:
        import uvicorn.config as _uvicorn_config

        _uvicorn_config.LOGGING_CONFIG.clear()
        _uvicorn_config.LOGGING_CONFIG.update(cfg)
    except ImportError:
        # uvicorn not installed (e.g. in tests that don't import the app)
        pass
