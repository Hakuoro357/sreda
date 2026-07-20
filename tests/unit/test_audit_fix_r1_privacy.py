"""R1-фиксы аудита 2026-07-18, область W3 (privacy trio + admin audit).

Покрывает находки decision-log R1:

- C5  privacy_guard   — compound-маркеры URL (`/apikey/<SECRET>`) режутся;
                        обычные слова («monkey»/«keyboard») — нет.
- C8  weather_tool    — лог ошибок геокодинга/прогноза БЕЗ URL (name/lat/lon);
                        только класс исключения + HTTP-статус.
- M15 outbox_delivery — 401/403 = retryable/systemic, НЕ permanent dead-letter.
- M16 outbox_delivery — алерт retry-exhausted БЕЗ str(exc) (chat_id/URL);
                        только класс + http_status.
- M20 admin.routes    — `_audit_admin_view` коммитит запись чтения PII
                        (privileged_session на выходе не коммитит).

Без сети и без Postgres: SQLite in-memory + monkeypatch httpx/alerts.
"""

from __future__ import annotations

import logging
import types

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
import sreda.db.models  # noqa: F401 — регистрирует все таблицы на Base.metadata
from sreda.db.models.audit import AuditLog
from sreda.services import weather_tool
from sreda.services.privacy_guard import RegexPrivacyGuard
from sreda.workers.outbox_delivery import OutboxDeliveryWorker


# ---------------------------------------------------------------------------
# C5 — privacy_guard: compound-маркеры секретов в пути URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example.com/apikey/abc123def456ghi",
        "https://api.example.com/apiKey/abc123def456ghi",       # camelCase → collapse
        "https://api.example.com/secretkey/abc123def456ghi",
        "https://api.example.com/secretKey/abc123def456ghi",
        "https://api.example.com/apitoken/abc123def456ghi",
        "https://api.example.com/accessToken/abc123def456ghi",
        "https://api.example.com/authtoken/abc123def456ghi",
        "https://api.example.com/refreshToken/abc123def456ghi",
        "https://api.example.com/privatekey/abc123def456ghi",
        "https://api.example.com/accessKey/abc123def456ghi",
        "https://api.example.com/clientSecret/abc123def456ghi",
    ],
)
def test_compound_secret_url_markers_are_redacted(url: str) -> None:
    """URL со слитным/camelCase маркером в ПУТИ (без query) маскируется.

    До R1 токенизатор давал сегмент вида `secretkey`/`apitoken`, которого не
    было в наборе маркеров (составные части `secret`/`token` по-отдельности
    внутри слова НЕ давали word-boundary и в credential-правиле :170 тоже не
    матчились), и секрет утекал открытым в LLM-контекст.

    Замечание по покрытию: `apikey`/`apiKey` дополнительно ловятся более
    старым credential-правилом (`api[_ -]?key`, :170) — belt-and-suspenders;
    новые URL-маркеры закрывают ИМЕННО остальные 9 составных (`secretkey`,
    `apitoken`, `accessToken`, `authtoken`, `refreshToken`, `privatekey`,
    `accessKey`, `clientSecret`), которые то правило пропускает (проверено:
    без фикса эти 9 кейсов RED). Плюс маркер `apikey` закрывает краевой
    случай короткого значения (<3 симв.), который credential-правило с
    порогом `{3,}` не редактирует.
    """
    guard = RegexPrivacyGuard()
    res = guard.sanitize_text(f"смотри {url}")
    assert res is not None
    assert "[url]" in res.sanitized_text, url
    assert url not in res.sanitized_text, url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/monkey/business",
        "https://example.com/keyboard/shortcuts",
        "https://example.com/design/article",
        "https://example.com/keynote/2026",
        "https://example.com/donkey/farm",
    ],
)
def test_benign_compound_words_are_not_redacted(url: str) -> None:
    """Слова, СОДЕРЖАЩИЕ подстроку маркера («monkey»⊃«key»), но не равные
    ни одному маркеру целиком — НЕ триггерят redaction (нет over-redaction
    разговорного контента, уходящего в LLM)."""
    guard = RegexPrivacyGuard()
    res = guard.sanitize_text(f"смотри {url}")
    assert res is not None
    assert "[url]" not in res.sanitized_text, url
    assert url in res.sanitized_text, url


# ---------------------------------------------------------------------------
# C8 — weather_tool: логи ошибок без URL (name/lat/lon)
# ---------------------------------------------------------------------------


def _http_status_error(url: str, status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    response = httpx.Response(status_code=status, request=request)
    # str(exc) намеренно содержит полный URL — воспроизводим утечку, которую
    # фикс обязан НЕ логировать.
    return httpx.HTTPStatusError(
        f"Client error '{status}' for url '{url}'", request=request, response=response
    )


def test_geocode_error_log_omits_location_url(monkeypatch, caplog) -> None:
    leaky_url = (
        "https://geocoding-api.open-meteo.com/v1/search"
        "?name=СекретныйГородТестоградАльфа&language=ru&count=1"
    )

    def _raising_get(*_args, **_kwargs):
        raise _http_status_error(leaky_url, 400)

    monkeypatch.setattr(weather_tool.httpx, "get", _raising_get)

    with caplog.at_level(logging.WARNING, logger="sreda.services.weather_tool"):
        result = weather_tool._geocode("Тестоград Альфа")

    assert result is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "ожидали WARNING от geocode"
    msg = " ".join(r.getMessage() for r in warnings)
    # URL / имя города НЕ в логах.
    assert "geocoding-api" not in msg
    assert "name=" not in msg
    assert "СекретныйГород" not in msg
    # Класс исключения + HTTP-статус — есть.
    assert "HTTPStatusError" in msg
    assert "400" in msg


def test_forecast_error_log_omits_coordinates_url(monkeypatch, caplog) -> None:
    leaky_url = (
        "https://api.open-meteo.com/v1/forecast"
        "?latitude=55.751244&longitude=37.618423&timezone=Europe/Moscow"
    )

    def _raising_get(*_args, **_kwargs):
        raise _http_status_error(leaky_url, 429)

    monkeypatch.setattr(weather_tool.httpx, "get", _raising_get)

    with caplog.at_level(logging.WARNING, logger="sreda.services.weather_tool"):
        result = weather_tool._fetch_forecast(
            55.751244, 37.618423, "Europe/Moscow", "daily", 1,
        )

    assert result is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "ожидали WARNING от forecast"
    msg = " ".join(r.getMessage() for r in warnings)
    # Координаты / URL НЕ в логах.
    assert "55.751244" not in msg
    assert "37.618423" not in msg
    assert "open-meteo" not in msg
    assert "latitude=" not in msg
    # Класс + статус — есть.
    assert "HTTPStatusError" in msg
    assert "429" in msg


# ---------------------------------------------------------------------------
# M15 — outbox_delivery: 401/403 НЕ permanent (retryable/systemic)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [400, 404, 410, 422])
def test_permanent_delivery_errors_still_permanent(status_code: int) -> None:
    assert OutboxDeliveryWorker._is_permanent_delivery_error(status_code) is True


@pytest.mark.parametrize("status_code", [401, 403, 429, 500, 502, 503, None])
def test_retryable_delivery_errors_not_permanent(status_code) -> None:
    """401 (протухший токен) / 403 (bot blocked/kicked) — состояние КАНАЛА,
    восстановимо → обычный retry-цикл с потолком, НЕ мгновенный dead-letter.
    429/5xx/сеть (None) — транзиентные, как и раньше."""
    assert OutboxDeliveryWorker._is_permanent_delivery_error(status_code) is False


# ---------------------------------------------------------------------------
# M16 — outbox_delivery: алерт retry-exhausted без chat_id/URL
# ---------------------------------------------------------------------------


class _FakeDeliveryError(Exception):
    def __init__(self, msg: str, status_code: int) -> None:
        super().__init__(msg)
        self.status_code = status_code


def test_retry_exhausted_alert_omits_exception_body(monkeypatch) -> None:
    captured: dict = {}

    def _fake_send_admin_alert(severity, title, details, *, dedupe_key=None):
        captured["severity"] = severity
        captured["title"] = title
        captured["details"] = details
        captured["dedupe_key"] = dedupe_key

    monkeypatch.setattr(
        "sreda.services.admin_alerts.send_admin_alert", _fake_send_admin_alert,
    )

    # str(exc) содержит chat_id и URL — канальный PII, НЕ должен попасть в алерт.
    exc = _FakeDeliveryError(
        "POST https://api.telegram.org/bot123:ABC/sendMessage"
        "?chat_id=987654321 -> 403 Forbidden",
        status_code=403,
    )
    row = types.SimpleNamespace(id="out_test_row", feature_key="reminder")

    OutboxDeliveryWorker._alert_retry_exhausted(row, channel="telegram", exc=exc)

    details = captured["details"]
    # PII (chat_id) и URL НЕ в алерте.
    assert "987654321" not in details
    assert "api.telegram.org" not in details
    assert "sendMessage" not in details
    # Класс исключения + http_status — есть.
    assert "_FakeDeliveryError" in details
    assert "403" in details
    assert captured["dedupe_key"] == "outbox-retry-exhausted:telegram"


# ---------------------------------------------------------------------------
# M20 — admin.routes: _audit_admin_view коммитит запись чтения PII
# ---------------------------------------------------------------------------


def test_audit_admin_view_commits_read_trail(tmp_path) -> None:
    """privileged_session на выходе делает session.close() без commit —
    с commit=False (default audit_event) аудит-запись чтения PII молча
    откатывалась бы. Фикс: helper пишет commit=True → строка durable
    (переживает последующий rollback)."""
    from sreda.admin.routes import _audit_admin_view

    engine = create_engine(f"sqlite:///{tmp_path}/audit_m20.db")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        request = types.SimpleNamespace(
            headers={"x-forwarded-for": "", "user-agent": "pytest-agent"},
            client=types.SimpleNamespace(host="127.0.0.1"),
        )
        _audit_admin_view(
            session, "admin.dashboard.viewed", "admin_tg:123", request,
        )
        # Ключ теста: rollback НЕ должен стереть запись — она уже закоммичена.
        session.rollback()
        rows = session.query(AuditLog).all()
        assert len(rows) == 1
        assert rows[0].action == "admin.dashboard.viewed"
        assert rows[0].actor_id == "admin_tg:123"
        assert rows[0].actor_type == "admin"
    finally:
        session.close()
