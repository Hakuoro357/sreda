"""Unit tests for Phase 3 of the second-Telegram-bot feature.

Covers:
  - Advisory lock id derivation: stable + distinct for sreda vs sreda_home
  - Per-bot poller channel key format
  - --bot-key unknown → fail-closed (registry KeyError → exit 1)
  - SREDA_TELEGRAM_BOT_KEY env var is picked up
  - Migration: channel column is now length 64 (assert via SQLAlchemy metadata)
  - --check-config with stubbed registry+getMe:
      * username mismatch → hard fail (rc=1)
      * token without username → hard fail (rc=1)
      * all OK → rc=0
      * duplicate tokens → hard fail (rc=1)
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sreda.db.models  # noqa: F401 — register all models
from sreda.config.bot_registry import BotConfig, TelegramBotRegistry
from sreda.db.base import Base
from sreda.db.session import get_engine, get_session_factory
from sreda.config.settings import get_settings
from sreda.workers import telegram_long_poll as tlp
from sreda.workers.telegram_long_poll import (
    _advisory_lock_id,
    _poller_channel,
)


# ---- Fixtures ----------------------------------------------------------


@pytest.fixture
def fresh_db(monkeypatch, tmp_path: Path):
    """Build an empty SQLite DB with the current schema (String(64) channel)."""
    db_path = tmp_path / "test.db"
    key = base64.urlsafe_b64encode(
        b"0123456789abcdef0123456789abcdef"
    ).decode("ascii")
    monkeypatch.setenv("SREDA_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", key)
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_TOKEN", "test-token-sreda")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    Base.metadata.create_all(get_engine())
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


# ---- Advisory lock derivation ------------------------------------------


def test_advisory_lock_ids_are_stable():
    """Same bot_key produces the same lock id across calls."""
    id1 = _advisory_lock_id("sreda")
    id2 = _advisory_lock_id("sreda")
    assert id1 == id2


def test_advisory_lock_ids_are_distinct():
    """Different bot_keys produce different lock ids (no collision)."""
    id_sreda = _advisory_lock_id("sreda")
    id_home = _advisory_lock_id("sreda_home")
    assert id_sreda != id_home


def test_advisory_lock_id_is_signed_64bit():
    """Lock ids are in the signed 64-bit range accepted by pg_advisory_lock."""
    for key in ("sreda", "sreda_home", "test_bot_123"):
        lid = _advisory_lock_id(key)
        assert isinstance(lid, int)
        assert -(2 ** 63) <= lid < 2 ** 63


def test_advisory_lock_does_not_use_python_hash():
    """The lock id must be identical regardless of PYTHONHASHSEED.

    We can't vary PYTHONHASHSEED at runtime, but we can assert the result
    matches the expected SHA-256-based value computed from scratch inline.
    """
    import hashlib
    for key in ("sreda", "sreda_home"):
        expected_bytes = hashlib.sha256(
            f"sreda-telegram-poller:{key}".encode()
        ).digest()[:8]
        expected = int.from_bytes(expected_bytes, "big", signed=True)
        assert _advisory_lock_id(key) == expected


# ---- Per-bot channel key -----------------------------------------------


def test_poller_channel_format():
    """Channel key must be 'telegram:<bot_key>'."""
    assert _poller_channel("sreda") == "telegram:sreda"
    assert _poller_channel("sreda_home") == "telegram:sreda_home"


def test_poller_channel_fits_in_string64():
    """Even the longest expected key fits within String(64)."""
    for key in ("sreda", "sreda_home"):
        channel = _poller_channel(key)
        assert len(channel) <= 64


def test_poller_uses_per_bot_channel(fresh_db):
    """TelegramLongPoller sets _channel to 'telegram:<bot_key>'."""
    poller = tlp.TelegramLongPoller("test-token", bot_key="sreda_home")
    assert poller._channel == "telegram:sreda_home"


# ---- Migration: String(64) channel column length -----------------------


def test_channel_column_length_is_64(fresh_db):
    """poller_offsets and poller_heartbeats channel columns must be String(64).

    Asserted via SQLAlchemy metadata so the test is dialect-agnostic and
    mirrors what the ORM uses when creating test DBs (SQLite).
    """
    from sqlalchemy import inspect as sa_inspect
    from sreda.db.models.poller_state import PollerOffset, PollerHeartbeat

    engine = get_engine()
    insp = sa_inspect(engine)

    for table_name, model in [
        ("poller_offsets", PollerOffset),
        ("poller_heartbeats", PollerHeartbeat),
    ]:
        cols = {c["name"]: c for c in insp.get_columns(table_name)}
        assert "channel" in cols, f"{table_name}.channel column missing"
        col_type = cols["channel"]["type"]
        # The type length is set on the mapped column.
        assert col_type.length == 64, (
            f"{table_name}.channel expected String(64), got String({col_type.length})"
        )


# ---- --bot-key unknown → fail-closed -----------------------------------


@pytest.mark.asyncio
async def test_unknown_bot_key_returns_exit_1(fresh_db, monkeypatch):
    """--bot-key with an unregistered key must return exit code 1 (fail-closed)."""
    # Only "sreda" is in the registry (SREDA_HOME_BOT_TOKEN not set).
    rc = await tlp._amain(["--bot-key", "does_not_exist"])
    assert rc == 1


@pytest.mark.asyncio
async def test_env_bot_key_unknown_returns_exit_1(fresh_db, monkeypatch):
    """SREDA_TELEGRAM_BOT_KEY with an unknown value must return exit code 1."""
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_KEY", "ghost_bot")
    rc = await tlp._amain([])
    assert rc == 1


# ---- SREDA_TELEGRAM_BOT_KEY env var ------------------------------------


@pytest.mark.asyncio
async def test_env_bot_key_overrides_default(fresh_db, monkeypatch):
    """SREDA_TELEGRAM_BOT_KEY is read and fail-closed on unknown key."""
    monkeypatch.setenv("SREDA_TELEGRAM_BOT_KEY", "unknown_from_env")
    rc = await tlp._amain([])
    assert rc == 1  # unknown key → fail-closed, rc=1


# ---- --check-config path -----------------------------------------------


def _make_registry_with_username(token: str, username: str) -> TelegramBotRegistry:
    return TelegramBotRegistry(
        [BotConfig(key="sreda", token=token, username=username)],
        system_default_bot_key="sreda",
        admin_bot_key="sreda",
    )


def _make_registry_no_username(token: str) -> TelegramBotRegistry:
    return TelegramBotRegistry(
        [BotConfig(key="sreda", token=token, username=None)],
        system_default_bot_key="sreda",
        admin_bot_key="sreda",
    )


@pytest.mark.asyncio
async def test_check_config_username_mismatch_returns_exit_1(fresh_db, monkeypatch):
    """--check-config: getMe returns different username → hard fail (rc=1)."""
    registry = _make_registry_with_username("tok-1", "ExpectedBot")

    # getMe returns a different username (token is swapped / misconfigured).
    async def fake_get_me() -> dict:
        return {"ok": True, "result": {"username": "WrongBot", "id": 1}}

    fake_client = MagicMock()
    fake_client.get_me = fake_get_me

    # Patch TelegramBotRegistry at its definition site so the local import
    # inside _amain picks up the mock.
    with patch("sreda.config.bot_registry.TelegramBotRegistry") as mock_reg_cls:
        mock_reg_cls.from_settings.return_value = registry
        with patch("sreda.config.bot_registry.telegram_client_for", return_value=fake_client):
            rc = await tlp._amain(["--check-config", "--bot-key", "sreda"])

    assert rc == 1


@pytest.mark.asyncio
async def test_check_config_token_without_username_returns_exit_1(fresh_db, monkeypatch):
    """--check-config: bot has token but no username → hard fail (rc=1).

    Without username the swap-check can't run silently, so we refuse to
    proceed rather than allowing a misconfigured bot to start polling.
    """
    registry = _make_registry_no_username("tok-noname")

    with patch("sreda.config.bot_registry.TelegramBotRegistry") as mock_reg_cls:
        mock_reg_cls.from_settings.return_value = registry
        rc = await tlp._amain(["--check-config", "--bot-key", "sreda"])

    assert rc == 1


@pytest.mark.asyncio
async def test_check_config_all_ok_returns_0(fresh_db, monkeypatch):
    """--check-config: correct username from getMe → rc=0 (lock released)."""
    registry = _make_registry_with_username("tok-ok", "SredaBot")

    async def fake_get_me() -> dict:
        return {"ok": True, "result": {"username": "SredaBot", "id": 1}}

    fake_client = MagicMock()
    fake_client.get_me = fake_get_me

    # Stub the advisory lock so SQLite test doesn't need pg_try_advisory_lock.
    fake_engine = MagicMock()
    fake_conn = MagicMock()
    fake_conn.execute.return_value.scalar.return_value = True  # lock granted
    fake_engine.connect.return_value = fake_conn

    with patch("sreda.config.bot_registry.TelegramBotRegistry") as mock_reg_cls:
        mock_reg_cls.from_settings.return_value = registry
        with patch("sreda.config.bot_registry.telegram_client_for", return_value=fake_client):
            with patch.object(tlp, "create_engine", return_value=fake_engine):
                rc = await tlp._amain(["--check-config", "--bot-key", "sreda"])

    assert rc == 0
    # Lock must have been acquired and then released (shutdown called).
    assert fake_conn.execute.call_count >= 2  # lock + unlock


@pytest.mark.asyncio
async def test_check_config_duplicate_token_returns_exit_1(fresh_db, monkeypatch):
    """--check-config: two bots with same token → hard fail (rc=1)."""
    registry = TelegramBotRegistry(
        [
            BotConfig(key="sreda", token="same-token", username="BotA"),
            BotConfig(key="sreda_home", token="same-token", username="BotB"),
        ],
        system_default_bot_key="sreda",
        admin_bot_key="sreda",
    )

    async def fake_get_me() -> dict:
        return {"ok": True, "result": {"username": "BotA", "id": 1}}

    fake_client = MagicMock()
    fake_client.get_me = fake_get_me

    with patch("sreda.config.bot_registry.TelegramBotRegistry") as mock_reg_cls:
        mock_reg_cls.from_settings.return_value = registry
        with patch("sreda.config.bot_registry.telegram_client_for", return_value=fake_client):
            rc = await tlp._amain(["--check-config", "--bot-key", "sreda"])

    assert rc == 1


# ---- Two bots coexist: distinct lock ids and channels ------------------


def test_two_bots_have_distinct_lock_ids_and_channels():
    """sreda and sreda_home pollers use different lock ids and channel keys."""
    lock_sreda = _advisory_lock_id("sreda")
    lock_home = _advisory_lock_id("sreda_home")
    chan_sreda = _poller_channel("sreda")
    chan_home = _poller_channel("sreda_home")

    assert lock_sreda != lock_home, "lock ids must differ"
    assert chan_sreda != chan_home, "channel keys must differ"
    assert chan_sreda == "telegram:sreda"
    assert chan_home == "telegram:sreda_home"
