"""R-28: admin_alerts service tests.

Covers:
- burst cap (process-wide 10/min, ordering AFTER dedup per Codex R1 M1)
- default dedupe_key derivation (deterministic)
- settings missing — silent skip
- Telegram POST failure swallowed
- DB dedup logic (real SQLite + ON CONFLICT) — two-phase attempt/mark per Codex R1 M2
- severity-specific rate windows (P0=60s, P1=300s)
- daemon thread fire-and-forget (mocked sync для определённости в тестах)

Tests reset module-level burst window between cases via fixture.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from sreda.db.base import Base
from sreda.db.models import (  # noqa: F401 — register all tables for create_all
    AdminAlertSeen,
)
from sreda.db.session import get_engine, get_session_factory
from sreda.config.settings import get_settings
from sreda.services import admin_alerts as aa
from sreda.services.admin_alerts import (
    _BURST_CAP_PER_MINUTE,
    _check_burst_cap,
    _default_dedupe_key,
    _mark_sent,
    _should_attempt_per_dedupe,
    send_admin_alert,
)


@pytest.fixture(autouse=True)
def _reset_module_state() -> None:
    """Reset module-level burst counter + in-flight registry before each test."""
    aa._burst_window.clear()
    with aa._in_flight_lock:
        aa._in_flight.clear()


@pytest.fixture(autouse=True)
def _sync_thread(monkeypatch, request) -> None:
    """Replace threading.Thread в admin_alerts с sync runner.

    Daemon thread spawn делает tests non-deterministic. Sync override:
    create a fake Thread that runs target() immediately on start().

    NOTE: ``threading.Thread`` is a singleton module attribute — patching
    here affects the global ``threading`` module. That breaks
    ``asyncio.to_thread`` (used by ``alert_admin_async``) because asyncio's
    default executor spawns workers via ``threading.Thread``. Tests that
    exercise the async helper opt out by marking with
    ``@pytest.mark.no_sync_thread``.
    """
    if "no_sync_thread" in request.keywords:
        yield
        return

    class _SyncThread:
        def __init__(self, target=None, kwargs=None, **_):
            self._target = target
            self._kwargs = kwargs or {}

        def start(self) -> None:
            self._target(**self._kwargs)

    monkeypatch.setattr(
        "sreda.services.admin_alerts.threading.Thread", _SyncThread
    )
    yield


@pytest.fixture
def _db(monkeypatch, tmp_path):
    """Create SQLite DB с admin_alerts_seen table, return session_factory.

    Codex R5 MEDIUM fix: explicitly clear MAX env vars so legacy tests
    don't accidentally hit MAX path if dev shell has SREDA_MAX_BOT_TOKEN
    set. TG-only path is the desired baseline for these tests.
    """
    db_path = tmp_path / "admin_alerts_test.db"
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("SREDA_ADMIN_TELEGRAM_CHAT_ID", "123456789")
    monkeypatch.setenv("SREDA_MAX_BOT_TOKEN", "")
    monkeypatch.setenv("SREDA_ADMIN_MAX_CHAT_ID", "")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    Base.metadata.create_all(get_engine())
    yield get_session_factory()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


# ── (1) burst cap ──────────────────────────────────────────────────


def test_burst_cap_allows_up_to_limit() -> None:
    """First 10 calls within 60s allowed, 11th capped."""
    for i in range(_BURST_CAP_PER_MINUTE):
        assert _check_burst_cap() is True, f"call {i} should pass"
    assert _check_burst_cap() is False


def test_burst_cap_resets_after_window() -> None:
    """Entries older than 60s pruned — fresh send allowed."""
    import time as _time
    aa._burst_window = [_time.monotonic() - 70.0] * _BURST_CAP_PER_MINUTE
    assert _check_burst_cap() is True


# ── (2) default dedupe key ─────────────────────────────────────────


def test_default_dedupe_key_deterministic() -> None:
    """Same severity+title → same key. Different inputs → different keys."""
    k1 = _default_dedupe_key("P1", "LLM fallback engaged: BadRequestError")
    k2 = _default_dedupe_key("P1", "LLM fallback engaged: BadRequestError")
    k3 = _default_dedupe_key("P0", "LLM fallback engaged: BadRequestError")
    k4 = _default_dedupe_key("P1", "Different title")
    assert k1 == k2
    assert k1 != k3
    assert k1 != k4
    assert k1.startswith("auto:P1:")


# ── (3) settings missing → silent skip ──────────────────────────────


def test_silent_skip_when_no_bot_token(monkeypatch) -> None:
    """No telegram_bot_token → no httpx call, no exception.

    Codex R5 MEDIUM: explicitly clear MAX env so test isolation holds.
    """
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("SREDA_ADMIN_TELEGRAM_CHAT_ID", "123456789")
    monkeypatch.setenv("SREDA_MAX_BOT_TOKEN", "")
    monkeypatch.setenv("SREDA_ADMIN_MAX_CHAT_ID", "")
    get_settings.cache_clear()
    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        send_admin_alert(severity="P1", title="test", body="test body")
        mock_post.assert_not_called()


def test_silent_skip_when_no_chat_id(monkeypatch) -> None:
    """No admin_telegram_chat_id → silent skip."""
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("SREDA_ADMIN_TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("SREDA_MAX_BOT_TOKEN", "")
    monkeypatch.setenv("SREDA_ADMIN_MAX_CHAT_ID", "")
    get_settings.cache_clear()
    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        send_admin_alert(severity="P1", title="test", body="test body")
        mock_post.assert_not_called()


# ── (4) Telegram POST failure swallowed ─────────────────────────────


def test_telegram_post_failure_swallowed(_db) -> None:
    """httpx.post raises → no exception bubbles up. Caller continues."""
    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        mock_post.side_effect = Exception("network exploded")
        send_admin_alert(severity="P1", title="test", body="test body")
        mock_post.assert_called_once()


def test_telegram_returns_500_swallowed(_db) -> None:
    """HTTP 500 from Telegram → still no raise."""
    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=500, text="internal server error"
        )
        send_admin_alert(severity="P1", title="test", body="test body")
        mock_post.assert_called_once()


# ── (5) DB dedup logic ──────────────────────────────────────────────


def test_dedupe_first_attempt_allowed(_db) -> None:
    """First attempt — row created with _EPOCH last_sent_at → should_attempt=True."""
    should, occ = _should_attempt_per_dedupe("test:key1", "P1", "Test Title")
    assert should is True
    assert occ == 1


def test_dedupe_attempt_count_increments(_db) -> None:
    """Each call bumps occurrence_count regardless of should_attempt."""
    _, occ1 = _should_attempt_per_dedupe("test:key2", "P1", "Title")
    _, occ2 = _should_attempt_per_dedupe("test:key2", "P1", "Title")
    _, occ3 = _should_attempt_per_dedupe("test:key2", "P1", "Title")
    assert occ1 == 1
    assert occ2 == 2
    assert occ3 == 3


def test_dedupe_suppressed_after_mark_sent_within_window(_db) -> None:
    """After _mark_sent, next attempt within 5min window → should_attempt=False."""
    should1, _ = _should_attempt_per_dedupe("test:key3", "P1", "Title")
    assert should1 is True
    _mark_sent("test:key3")  # simulate successful POST
    should2, occ2 = _should_attempt_per_dedupe("test:key3", "P1", "Title")
    assert should2 is False  # rate-limited
    assert occ2 == 2  # count still increments


def test_dedupe_re_attempt_after_window_passes(_db) -> None:
    """After mark + age push, next attempt allowed (window passed)."""
    _should_attempt_per_dedupe("test:key4", "P1", "Title")
    _mark_sent("test:key4")

    # Age last_sent_at past 5min window
    sf = _db
    from sqlalchemy import text
    with sf() as session:
        session.execute(
            text("UPDATE admin_alerts_seen SET last_sent_at = :old WHERE dedupe_key = :k"),
            {"old": datetime.now(timezone.utc) - timedelta(seconds=400), "k": "test:key4"},
        )
        session.commit()

    should2, _ = _should_attempt_per_dedupe("test:key4", "P1", "Title")
    assert should2 is True  # window elapsed → re-fire


def test_dedupe_p0_60s_window(_db) -> None:
    """P0 has 60s window — within 60s + after mark_sent → suppressed."""
    _should_attempt_per_dedupe("test:key5", "P0", "Title")
    _mark_sent("test:key5")
    should2, _ = _should_attempt_per_dedupe("test:key5", "P0", "Title")
    assert should2 is False


def test_dedupe_retry_on_post_failure(_db) -> None:
    """If _mark_sent НЕ called (POST failed), next attempt still allowed.

    Codex R1 M2: previous behavior marked last_sent_at upfront → POST fail
    suppressed all retries within window. New behavior: only mark on success.
    """
    should1, _ = _should_attempt_per_dedupe("test:key6", "P1", "Title")
    assert should1 is True
    # Simulate POST failure: _mark_sent NOT called
    should2, _ = _should_attempt_per_dedupe("test:key6", "P1", "Title")
    assert should2 is True  # retry allowed because last_sent_at still _EPOCH


# ── (6) end-to-end with mocked POST ────────────────────────────────


def test_send_admin_alert_full_path(_db) -> None:
    """Happy path: settings OK + dedup OK → POST called с правильным payload."""
    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, text="ok")
        send_admin_alert(
            severity="P0",
            title="LLM fallback engaged: BadRequestError",
            body="tenant: t1\nreason: x",
            dedupe_key="llm_fallback:BadRequestError:housewife",
        )
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        assert "api.telegram.org" in mock_post.call_args.args[0]
        payload = call_kwargs["json"]
        assert payload["chat_id"] == "123456789"
        assert "🔥 [SREDA P0]" in payload["text"]
        assert "LLM fallback engaged: BadRequestError" in payload["text"]
        assert "tenant: t1" in payload["text"]


def test_send_admin_alert_dedup_suppresses_second(_db) -> None:
    """Second call with same dedupe_key suppressed → no second POST."""
    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, text="ok")
        send_admin_alert(
            severity="P1", title="Test", body="body1", dedupe_key="same_key",
        )
        send_admin_alert(
            severity="P1", title="Test", body="body2", dedupe_key="same_key",
        )
        assert mock_post.call_count == 1


def test_send_admin_alert_burst_cap_drops_excess(_db) -> None:
    """11 distinct dedupe_keys → first 10 POST, 11th burst-capped."""
    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, text="ok")
        for i in range(_BURST_CAP_PER_MINUTE + 1):
            send_admin_alert(
                severity="P1",
                title=f"Test #{i}",
                body=f"body {i}",
                dedupe_key=f"key_{i}",
            )
        assert mock_post.call_count == _BURST_CAP_PER_MINUTE


def test_send_admin_alert_dedup_does_not_burn_burst_quota(_db) -> None:
    """Codex R1 M1: 10 dedup-suppressed calls + 1 fresh call → fresh sends.

    Previous order (burst BEFORE dedup) would burn burst quota на rate-limited
    repeats, blocking new distinct alerts. New order (dedup BEFORE burst)
    fixes это.
    """
    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, text="ok")
        # Send 10x same key — only first goes through, rest dedup-suppressed.
        for _ in range(10):
            send_admin_alert(
                severity="P1", title="Repeat", body="x", dedupe_key="hot_key",
            )
        # 11 фresh distinct keys — should all send (burst quota intact)
        for i in range(11):
            send_admin_alert(
                severity="P1", title=f"Fresh {i}", body="x",
                dedupe_key=f"fresh_{i}",
            )
        # First send (hot_key) + 11 fresh = 12 POSTs… но burst cap=10.
        # 10 distinct fresh + 1 hot = 11 attempted POSTs, capped at 10.
        # Important: should be MORE than 1 (= old broken behavior would only POST first hot).
        assert mock_post.call_count == 10  # burst cap reached after 10 distinct sends


def test_send_admin_alert_failure_allows_retry_next_occurrence(_db) -> None:
    """Codex R1 M2: POST fails → last_sent_at unchanged → next occurrence retries."""
    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        # First — POST fails
        mock_post.return_value = MagicMock(status_code=500, text="fail")
        send_admin_alert(
            severity="P1", title="Test", body="b1", dedupe_key="retry_key",
        )
        # Second — POST succeeds. Should be called (not suppressed).
        mock_post.return_value = MagicMock(status_code=200, text="ok")
        send_admin_alert(
            severity="P1", title="Test", body="b2", dedupe_key="retry_key",
        )
        assert mock_post.call_count == 2  # both attempted


# ── (7) Codex R2 MAJOR — concurrent race на same dedupe_key ───────


def test_in_flight_lease_blocks_concurrent_duplicate(_db) -> None:
    """Codex R2 MAJOR fix: concurrent threads с same dedupe_key — second blocked.

    Simulates race: 2 callers fire same dedupe_key одновременно. With sync
    thread monkeypatch + checked execution order, second call's
    `_try_reserve_in_flight` returns False (key held by first) → second
    skips POST.

    Real concurrent path (production): first thread held lease до POST
    completion. Second arrived during that window → suppressed.
    """
    # Pre-acquire lease для "first" thread (simulates first thread in middle of POST)
    assert aa._try_reserve_in_flight("hot_key") is True
    # Second arrival: lease held → blocked
    assert aa._try_reserve_in_flight("hot_key") is False
    # Release first → next attempt allowed
    aa._release_in_flight("hot_key")
    assert aa._try_reserve_in_flight("hot_key") is True


def test_in_flight_lease_expires_after_ttl(_db) -> None:
    """Lease auto-expires after _IN_FLIGHT_TTL_S (safety against crashed threads)."""
    import time
    aa._try_reserve_in_flight("ttl_key")
    # Manually push expiry into past (simulate 31s passed)
    with aa._in_flight_lock:
        aa._in_flight["ttl_key"] = time.monotonic() - 1.0
    # Next reserve should succeed (expired entry pruned)
    assert aa._try_reserve_in_flight("ttl_key") is True


def test_in_flight_release_idempotent() -> None:
    """_release_in_flight на missing key — no-op."""
    aa._release_in_flight("nonexistent_key")  # should not raise
    aa._try_reserve_in_flight("rel_key")
    aa._release_in_flight("rel_key")
    aa._release_in_flight("rel_key")  # second release safe


def test_send_admin_alert_concurrent_same_key_only_one_posts(_db) -> None:
    """Integration: 2 concurrent send_admin_alert с same key — only first POSTs.

    Pre-claim lease before second call → second's _try_reserve fails →
    POST skipped. Validates full integration через _deliver_in_thread.
    """
    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, text="ok")
        # Manually claim the key (simulates thread #1 in-flight)
        aa._try_reserve_in_flight("concurrent_key")
        # Second call arrives — should be suppressed by in-flight guard.
        send_admin_alert(
            severity="P1", title="Test", body="b", dedupe_key="concurrent_key",
        )
        # Concurrent first finishes (release manually)
        aa._release_in_flight("concurrent_key")
        # Mock should NOT have been called (in-flight blocked it)
        assert mock_post.call_count == 0


@pytest.fixture
def _db_max(monkeypatch, tmp_path):
    """Like _db but with MAX configured (primary) + TG (fallback)."""
    db_path = tmp_path / "admin_alerts_max_test.db"
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "test-tg-token")
    monkeypatch.setenv("SREDA_ADMIN_TELEGRAM_CHAT_ID", "111111111")
    monkeypatch.setenv("SREDA_MAX_BOT_TOKEN", "test-max-token")
    monkeypatch.setenv("SREDA_ADMIN_MAX_CHAT_ID", "320955459")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    Base.metadata.create_all(get_engine())
    yield get_session_factory()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


# ── (8) #395: DUAL channel contract + best-effort (было R-28 MAX-primary) ──


def test_max_payload_contract_in_dual(_db_max) -> None:
    """#395: дуал (TG+MAX). Контракт MAX-POST (2-й вызов) сохранён: chat_id в query,
    body только text (без chat_id), Authorization без Bearer."""
    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, text="ok")
        send_admin_alert(
            severity="P1", title="MAX path", body="b", dedupe_key="max_only",
        )
        # Дуал: оба канала POST'ятся (TG первым, MAX вторым).
        assert mock_post.call_count == 2
        max_call = next(c for c in mock_post.call_args_list
                        if "platform-api2.max.ru" in c.args[0])
        # Query string carries chat_id
        assert max_call.kwargs["params"]["chat_id"] == "320955459"
        # Body has text only (no chat_id)
        payload = max_call.kwargs["json"]
        assert "text" in payload
        assert "chat_id" not in payload
        # Auth header без Bearer
        assert max_call.kwargs["headers"]["Authorization"] == "test-max-token"


def test_dual_tg_delivers_when_max_500(_db_max) -> None:
    """#395 best-effort: MAX отдаёт 500 → TG (основной) всё равно доставил; оба канала
    попробованы (не «fallback», а параллельный дубль)."""
    def _post_side_effect(url, *args, **kwargs):
        if "platform-api2.max.ru" in url:
            return MagicMock(status_code=500, text="max down")
        return MagicMock(status_code=200, text="ok")

    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        mock_post.side_effect = _post_side_effect
        send_admin_alert(
            severity="P1", title="MAX500", body="b", dedupe_key="fb_key",
        )
        assert mock_post.call_count == 2
        urls = [c.args[0] for c in mock_post.call_args_list]
        assert any("platform-api2.max.ru" in u for u in urls)
        assert any("api.telegram.org" in u for u in urls)


def test_dual_tg_delivers_when_max_raises(_db_max) -> None:
    """#395 best-effort: MAX бросает (timeout/network) — исключение проглочено в
    _post_max_sync, TG (основной) доставил, ход не падает."""
    def _post_side_effect(url, *args, **kwargs):
        if "platform-api2.max.ru" in url:
            raise Exception("max timeout")
        return MagicMock(status_code=200, text="ok")

    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        mock_post.side_effect = _post_side_effect
        send_admin_alert(
            severity="P0", title="MAX dies", body="b", dedupe_key="exc_key",
        )
        assert mock_post.call_count == 2


def test_max_only_no_tg_configured(monkeypatch, tmp_path) -> None:
    """MAX configured but TG empty → MAX success no-op, MAX failure silently dropped."""
    db_path = tmp_path / "admin_alerts_max_only.db"
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("SREDA_ADMIN_TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("SREDA_MAX_BOT_TOKEN", "test-max-token")
    monkeypatch.setenv("SREDA_ADMIN_MAX_CHAT_ID", "320955459")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    Base.metadata.create_all(get_engine())
    try:
        with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=500, text="max down")
            send_admin_alert(
                severity="P1", title="MAX-only", body="b", dedupe_key="mo_key",
            )
            # MAX attempted once, no TG fallback (not configured).
            assert mock_post.call_count == 1
            assert "platform-api2.max.ru" in mock_post.call_args.args[0]
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


@pytest.mark.no_sync_thread
def test_alert_admin_async_dual_passes_correct_chat_ids(_db_max, monkeypatch) -> None:
    """#395: alert_admin_async дуалит — в КАЖДЫЙ канал уходит его chat_id
    (TG=111111111 основной, MAX=320955459 дубль), оба вызваны.

    Mocks ``_post_max_sync`` / ``_post_telegram_sync`` directly (rather than
    via httpx) — bypasses ``asyncio.to_thread`` interaction with the
    autouse ``_sync_thread`` fixture (which intentionally hijacks
    ``threading.Thread`` for the sync send_admin_alert tests).
    """
    import asyncio
    from sreda.services.admin_alerts import alert_admin_async

    max_calls: list[tuple] = []
    tg_calls: list[tuple] = []

    def _max_stub(token, chat, text):
        max_calls.append((token, chat, text))
        return True

    def _tg_stub(token, chat, text):
        tg_calls.append((token, chat, text))
        return True

    monkeypatch.setattr(aa, "_post_max_sync", _max_stub)
    monkeypatch.setattr(aa, "_post_telegram_sync", _tg_stub)

    result = asyncio.run(alert_admin_async("startup test message"))
    assert result is True
    # #395: оба канала получили (дубль), каждый — со своим chat_id.
    assert len(tg_calls) == 1
    assert tg_calls[0][1] == "111111111"   # TG chat_id (основной)
    assert len(max_calls) == 1
    assert max_calls[0][1] == "320955459"  # MAX chat_id (дубль)


@pytest.mark.no_sync_thread
def test_alert_admin_async_max_down_tg_delivers(_db_max, monkeypatch) -> None:
    """#395: alert_admin_async — MAX (дубль) падает → TG (основной) доставил;
    результат True (≥1 канал), оба попробованы."""
    import asyncio
    from sreda.services.admin_alerts import alert_admin_async

    max_calls: list[tuple] = []
    tg_calls: list[tuple] = []

    def _max_stub(token, chat, text):
        max_calls.append((token, chat, text))
        return False  # MAX failure

    def _tg_stub(token, chat, text):
        tg_calls.append((token, chat, text))
        return True

    monkeypatch.setattr(aa, "_post_max_sync", _max_stub)
    monkeypatch.setattr(aa, "_post_telegram_sync", _tg_stub)

    result = asyncio.run(alert_admin_async("test"))
    assert result is True
    assert len(max_calls) == 1
    assert len(tg_calls) == 1


@pytest.mark.no_sync_thread
def test_alert_admin_async_no_channel_returns_false(monkeypatch) -> None:
    """alert_admin_async: no channel configured → returns False, no POST."""
    import asyncio
    from sreda.services.admin_alerts import alert_admin_async

    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("SREDA_ADMIN_TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("SREDA_MAX_BOT_TOKEN", "")
    monkeypatch.setenv("SREDA_ADMIN_MAX_CHAT_ID", "")
    get_settings.cache_clear()

    max_calls: list[tuple] = []
    tg_calls: list[tuple] = []
    monkeypatch.setattr(aa, "_post_max_sync", lambda *a: max_calls.append(a) or True)
    monkeypatch.setattr(aa, "_post_telegram_sync", lambda *a: tg_calls.append(a) or True)

    result = asyncio.run(alert_admin_async("orphan"))
    assert result is False
    assert len(max_calls) == 0
    assert len(tg_calls) == 0


def test_no_channel_configured_silent_skip(monkeypatch) -> None:
    """Neither MAX nor TG configured → no POST, no exception."""
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("SREDA_ADMIN_TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("SREDA_MAX_BOT_TOKEN", "")
    monkeypatch.setenv("SREDA_ADMIN_MAX_CHAT_ID", "")
    get_settings.cache_clear()
    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        send_admin_alert(severity="P1", title="nowhere", body="b")
        mock_post.assert_not_called()


def test_no_stale_dedup_race_after_release(_db) -> None:
    """Codex R3 MAJOR regression: B can't POST duplicate based on stale dedup
    after A's mark_sent + release.

    Scenario (with reserve-AFTER-dedup, prior broken ordering):
    1. A reads dedup: last_sent_at=epoch → should_attempt=True
    2. B reads dedup: last_sent_at=epoch → should_attempt=True (stale)
    3. A reserves lease, POSTs, mark_sent (now), releases
    4. B reserves lease, POSTs — DUPLICATE based on stale decision

    With reserve-FIRST (current code), B blocked at lease step before
    dedup check, so B never holds a stale decision to act on. Once A
    releases, B is already returned. Test validates: after A's full
    cycle completes, a fresh send (simulating B's late arrival) sees
    updated last_sent_at и suppresses correctly.
    """
    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, text="ok")
        # Step 1: A fires fully — reserve, dedup, POST, mark_sent, release.
        send_admin_alert(
            severity="P1", title="X", body="a", dedupe_key="race_key",
        )
        assert mock_post.call_count == 1
        # In-flight lease released after A finished
        with aa._in_flight_lock:
            assert "race_key" not in aa._in_flight

        # Step 2: B fires AFTER A's release. Reserves OK (A released).
        # Then dedup check sees fresh last_sent_at → should_attempt=False.
        # Suppressed — no second POST.
        send_admin_alert(
            severity="P1", title="X", body="b", dedupe_key="race_key",
        )
        assert mock_post.call_count == 1  # still 1, not 2


# ── (9) #395: DUAL by default — Telegram primary + MAX duplicate ─────
#
# 2026-07-20: служебные алерты дублируются в ОБА канала — Telegram основной
# (первым), MAX дубль (вторым). Разворачивает R-28 MAX-primary → TG-fallback.
# Best-effort на канал; «доставлено» = долетело ХОТЯ БЫ в один.


def test_395_dual_delivers_both_channels(_db_max) -> None:
    """#395: оба канала сконфигурены → alert уходит в TG И в MAX (два POST)."""
    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, text="ok")
        send_admin_alert(
            severity="P1", title="dual", body="b", dedupe_key="dual_key",
        )
        assert mock_post.call_count == 2
        urls = [c.args[0] for c in mock_post.call_args_list]
        assert any("api.telegram.org" in u for u in urls)
        assert any("platform-api2.max.ru" in u for u in urls)


def test_395_telegram_is_primary_posted_first(_db_max) -> None:
    """#395: Telegram — основной, POST'ится ПЕРВЫМ; MAX (дубль) — вторым."""
    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, text="ok")
        send_admin_alert(
            severity="P1", title="order", body="b", dedupe_key="order_key",
        )
        assert mock_post.call_count == 2
        assert "api.telegram.org" in mock_post.call_args_list[0].args[0]
        assert "platform-api2.max.ru" in mock_post.call_args_list[1].args[0]


def test_395_tg_down_max_still_delivers_and_marks_sent(_db_max) -> None:
    """#395 best-effort: TG падает (500) → MAX всё равно получает; «доставлено»
    истинно при ≥1 канале (mark_sent сработал → повтор в окне подавлен целиком)."""
    def _side(url, *a, **k):
        if "api.telegram.org" in url:
            return MagicMock(status_code=500, text="tg down")
        return MagicMock(status_code=200, text="ok")

    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        mock_post.side_effect = _side
        send_admin_alert(
            severity="P1", title="tgfail", body="b", dedupe_key="tgfail_key",
        )
        # Оба канала попробованы (дуал), несмотря на падение TG.
        assert mock_post.call_count == 2
        # MAX доставил → delivered=True → mark_sent → второй с тем же ключом подавлен.
        mock_post.reset_mock()
        mock_post.side_effect = _side
        send_admin_alert(
            severity="P1", title="tgfail", body="b2", dedupe_key="tgfail_key",
        )
        assert mock_post.call_count == 0


def test_395_max_down_tg_still_delivers_and_marks_sent(_db_max) -> None:
    """#395 best-effort (зеркало): MAX (дубль) падает → TG (основной) доставил;
    «доставлено» истинно → повтор подавлен."""
    def _side(url, *a, **k):
        if "platform-api2.max.ru" in url:
            return MagicMock(status_code=500, text="max down")
        return MagicMock(status_code=200, text="ok")

    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        mock_post.side_effect = _side
        send_admin_alert(
            severity="P1", title="maxfail", body="b", dedupe_key="maxfail_key",
        )
        assert mock_post.call_count == 2
        mock_post.reset_mock()
        mock_post.side_effect = _side
        send_admin_alert(
            severity="P1", title="maxfail", body="b2", dedupe_key="maxfail_key",
        )
        assert mock_post.call_count == 0


def test_395_both_channels_fail_no_mark_sent_retries(_db_max) -> None:
    """#395: оба канала упали → last_sent_at не тронут → следующее вхождение ретраит
    (и снова дуалит)."""
    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=500, text="down")
        send_admin_alert(
            severity="P1", title="allfail", body="b1", dedupe_key="af_key",
        )
        assert mock_post.call_count == 2  # оба попробованы
        mock_post.reset_mock()
        mock_post.return_value = MagicMock(status_code=200, text="ok")
        send_admin_alert(
            severity="P1", title="allfail", body="b2", dedupe_key="af_key",
        )
        # Ретрай снова дуалит (оба ok на этот раз).
        assert mock_post.call_count == 2


def test_395_dedup_once_per_alert_across_both_channels(_db_max) -> None:
    """#395: один dedupe_key = один дедуп-чек на ОБА канала; повтор в окне подавлен
    целиком (не «TG подавлен, MAX прошёл»)."""
    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, text="ok")
        send_admin_alert(severity="P1", title="d", body="b1", dedupe_key="dk")
        send_admin_alert(severity="P1", title="d", body="b2", dedupe_key="dk")
        # Первый: дуал → 2 POST. Второй: подавлен целиком → 0. Итого 2.
        assert mock_post.call_count == 2


def test_395_single_channel_configured_delivers_alone(_db) -> None:
    """#395: сконфигурен ОДИН канал (TG-only фикстура _db) → уходит только туда, без
    ошибок про отсутствующий MAX."""
    with patch("sreda.services.admin_alerts.httpx.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200, text="ok")
        send_admin_alert(
            severity="P1", title="solo", body="b", dedupe_key="solo_key",
        )
        assert mock_post.call_count == 1
        assert "api.telegram.org" in mock_post.call_args.args[0]


@pytest.mark.no_sync_thread
def test_395_alert_admin_async_dual_tg_first_then_max(_db_max, monkeypatch) -> None:
    """#395: alert_admin_async (legacy-вход, его использует #376) тоже дуалит —
    TG первым, MAX вторым, оба вызваны."""
    import asyncio
    from sreda.services.admin_alerts import alert_admin_async

    order: list[str] = []
    monkeypatch.setattr(aa, "_post_telegram_sync", lambda *a: order.append("tg") or True)
    monkeypatch.setattr(aa, "_post_max_sync", lambda *a: order.append("max") or True)

    result = asyncio.run(alert_admin_async("m"))
    assert result is True
    assert order == ["tg", "max"]


@pytest.mark.no_sync_thread
def test_395_alert_admin_async_tg_down_max_delivers(_db_max, monkeypatch) -> None:
    """#395: alert_admin_async — TG падает → MAX всё равно доставляет; результат True
    (≥1 канал), оба попробованы."""
    import asyncio
    from sreda.services.admin_alerts import alert_admin_async

    calls: list[str] = []
    monkeypatch.setattr(aa, "_post_telegram_sync", lambda *a: calls.append("tg") or False)
    monkeypatch.setattr(aa, "_post_max_sync", lambda *a: calls.append("max") or True)

    result = asyncio.run(alert_admin_async("m"))
    assert result is True
    assert calls == ["tg", "max"]
