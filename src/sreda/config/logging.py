import logging
from logging.config import dictConfig


def _build_config(level_no: int) -> dict:
    # Rationale for the split handlers:
    #   * "default"  — app / uvicorn error logs, filtered by the
    #                  configured log level (WARNING on prod).
    #   * "access"   — HTTP access log, ALWAYS at INFO regardless of
    #                  the app log level. Access lines are forensic
    #                  signal (spot 500-storms, missing webhooks) and
    #                  cost nothing relative to the real traffic.
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
            "access": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "level": logging.INFO,
            },
        },
        "root": {"level": level_no, "handlers": ["default"]},
        "loggers": {
            # uvicorn.* are pinned at INFO so server-lifecycle lines
            # ("Started server process", "Application startup complete",
            # startup errors) always land in the log regardless of the
            # app-level SREDA_LOG_LEVEL. On prod WARNING these are
            # startup-only — a handful of lines per restart, not noise.
            # Both use the "access" handler so they pass its INFO gate.
            "uvicorn": {"level": logging.INFO, "handlers": ["access"], "propagate": False},
            "uvicorn.error": {"level": logging.INFO, "handlers": ["access"], "propagate": False},
            "uvicorn.access": {"level": logging.INFO, "handlers": ["access"], "propagate": False},
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
