"""#95 defense-in-depth: глобальный редактор секретов в логах + heartbeat.

Закрывает остаточные пути (g-039): НАШ logger.exception, строки
heartbeat, любой будущий логгер — независимо от пина httpx→WARNING.
"""
from __future__ import annotations

import logging

from sreda.config.log_redaction import (
    SecretRedactingFilter,
    redact_secrets,
)

_TOKEN = "8123456:AA_super_secret_token_DO_NOT_LOG_xx"
_URL = f"https://api.telegram.org/bot{_TOKEN}/getUpdates"


def test_redact_secrets_masks_bot_token() -> None:
    out = redact_secrets(f"HTTP Request: POST {_URL}")
    assert _TOKEN not in out
    assert "bot<redacted>" in out
    # идемпотентность
    assert redact_secrets(out) == out


def test_redact_secrets_passthrough_clean() -> None:
    assert redact_secrets("обычная строка без токена") == \
        "обычная строка без токена"
    assert redact_secrets("") == ""


def test_filter_redacts_formatted_message() -> None:
    """logger.warning('...%s', url) — токен в args не должен утечь."""
    rec = logging.LogRecord(
        name="x", level=logging.WARNING, pathname=__file__, lineno=1,
        msg="network error on %s", args=(_URL,), exc_info=None,
    )
    SecretRedactingFilter().filter(rec)
    assert _TOKEN not in rec.getMessage()
    assert "bot<redacted>" in rec.getMessage()


def test_filter_redacts_exc_text() -> None:
    rec = logging.LogRecord(
        name="x", level=logging.ERROR, pathname=__file__, lineno=1,
        msg="poller iteration error", args=None, exc_info=None,
    )
    rec.exc_text = f"Traceback...\nhttpx.ConnectError: {_URL}\n"
    SecretRedactingFilter().filter(rec)
    assert _TOKEN not in rec.exc_text


def test_configure_logging_attaches_filter() -> None:
    """После configure_logging корневые хендлеры редактируют токен."""
    import io

    from sreda.config.logging import configure_logging
    configure_logging("INFO")
    root = logging.getLogger()
    assert any(isinstance(f, SecretRedactingFilter)
               for h in root.handlers for f in h.filters), (
        "редактор не навешен на корневые хендлеры"
    )
    # сквозная проверка: эмитим запись с токеном в свой stream-хендлер,
    # несущий тот же фильтр
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.addFilter(SecretRedactingFilter())
    lg = logging.getLogger("sreda.test.redaction")
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        lg.warning("got %s", _URL)
    finally:
        lg.removeHandler(h)
    assert _TOKEN not in buf.getvalue()
