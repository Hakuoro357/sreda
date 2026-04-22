import logging
import warnings
from logging.config import dictConfig


def _suppress_third_party_warnings() -> None:
    """Silence third-party DeprecationWarning spam that fills prod logs.

    Observed 2026-04-22: ~350 lines / run of
      langchain_core.runnables.config:
      DeprecationWarning: 'asyncio.iscoroutinefunction' is deprecated
      and slated for removal in Python 3.16
    These are langchain's responsibility, not ours — filter them so
    our own grep for ERROR / WARNING stays readable.
    """
    warnings.filterwarnings(
        "ignore",
        category=DeprecationWarning,
        message=r"'asyncio\.iscoroutinefunction' is deprecated.*",
    )


def _build_config(
    level_no: int,
    feature_requests_log_path: str | None = None,
    trace_log_path: str | None = None,
) -> dict:
    # Rationale for the split handlers:
    #   * "default"  — app / uvicorn error logs, filtered by the
    #                  configured log level (WARNING on prod).
    #   * "access"   — HTTP access log + other operationally-important
    #                  signals (uvicorn lifecycle, LLM request trace),
    #                  ALWAYS at INFO regardless of app log level.
    #   * "feature_requests"  — dedicated file for user asks the bot
    #                  can't fulfil; enabled when
    #                  ``SREDA_FEATURE_REQUESTS_LOG_PATH`` is set.
    #   * "trace"    — dedicated file for end-to-end per-turn timing
    #                  blocks; enabled when ``SREDA_TRACE_LOG_PATH`` is
    #                  set. Admin UI surfaces it as the default log.
    handlers: dict = {
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
    }
    feature_requests_logger: dict = {
        "level": logging.INFO,
        # Always emit to the general access stream so grep still works.
        "handlers": ["access"],
        "propagate": False,
    }
    if feature_requests_log_path:
        handlers["feature_requests_file"] = {
            "class": "logging.FileHandler",
            "formatter": "default",
            "level": logging.INFO,
            "filename": feature_requests_log_path,
            "encoding": "utf-8",
        }
        feature_requests_logger["handlers"] = ["access", "feature_requests_file"]

    trace_logger: dict = {
        "level": logging.INFO,
        "handlers": ["access"],
        "propagate": False,
    }
    if trace_log_path:
        handlers["trace_file"] = {
            "class": "logging.FileHandler",
            # trace blocks carry their own timestamp in the header line,
            # so the formatter's leading ``asctime`` is redundant — but
            # keeping a consistent format across log files is more
            # useful for admin-UI scanning than saving 20 bytes/block.
            "formatter": "default",
            "level": logging.INFO,
            "filename": trace_log_path,
            "encoding": "utf-8",
        }
        trace_logger["handlers"] = ["access", "trace_file"]

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": handlers,
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
            # sreda.llm — request/response traces for the conversational
            # handler. Pinned at INFO so traces survive prod WARNING.
            # Troubleshooting "bot forgot context" / "LLM hallucinated"
            # starts here; without this, each turn is opaque.
            "sreda.llm": {"level": logging.INFO, "handlers": ["access"], "propagate": False},
            # sreda.feature_requests — user asks the bot can't fulfil.
            # Dedicated file (when configured) for product input; also
            # goes to the access stream so it's visible in the normal
            # uvicorn log during dev.
            "sreda.feature_requests": feature_requests_logger,
            # sreda.trace — end-to-end per-turn timing blocks
            # (entry → voice → LLM iters → outbox → delivery).
            # Block-per-turn, one logger.info call = one atomic block.
            "sreda.trace": trace_logger,
        },
    }


def configure_logging(
    level: str = "INFO",
    *,
    feature_requests_log_path: str | None = None,
    trace_log_path: str | None = None,
) -> None:
    """Install a timestamped stream handler for the root logger and for
    uvicorn's named loggers, and mutate ``uvicorn.config.LOGGING_CONFIG``
    in place so uvicorn's own ``Server.serve()`` — which re-applies
    ``LOGGING_CONFIG`` AFTER we configure the app — picks up our format.

    Without the in-place mutation, uvicorn's startup wipes out our
    dictConfig and the access log reverts to the default
    ``INFO:     1.2.3.4:5678 - "GET /..."`` (no timestamp).
    """
    _suppress_third_party_warnings()
    level_upper = level.upper()
    level_no = getattr(logging, level_upper, logging.INFO)
    cfg = _build_config(
        level_no,
        feature_requests_log_path=feature_requests_log_path,
        trace_log_path=trace_log_path,
    )
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
