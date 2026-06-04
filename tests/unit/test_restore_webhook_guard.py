"""Tests for Phase 8 gate: restore_webhook refuses to run when a poller is active.

Phase 8 requirement: restore_webhook.py must fail-closed if any
sreda-telegram-poller@*.service or the legacy sreda-telegram-poller.service
is enabled or active, unless --force-webhook-mode is passed.

We mock the subprocess.run calls that check systemd state so these tests
run without a real systemd environment.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from sreda.scripts import restore_webhook as _rw


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_systemctl_result(stdout: str = "", returncode: int = 1) -> MagicMock:
    """Build a fake CompletedProcess for subprocess.run."""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.stdout = stdout
    result.stderr = ""
    result.returncode = returncode
    return result


def _active_result() -> MagicMock:
    return _make_systemctl_result(stdout="active\n", returncode=0)


def _inactive_result() -> MagicMock:
    return _make_systemctl_result(stdout="inactive\n", returncode=1)


def _not_found_result() -> MagicMock:
    return _make_systemctl_result(stdout="not-found\n", returncode=1)


def _enabled_result() -> MagicMock:
    return _make_systemctl_result(stdout="enabled\n", returncode=0)


# ---------------------------------------------------------------------------
# _is_unit_enabled_or_active
# ---------------------------------------------------------------------------

def test_is_unit_active_returns_true():
    """Unit that is active → True."""
    with patch("sreda.scripts.restore_webhook.subprocess.run") as mock_run:
        # is-enabled returns not-found; is-active returns active
        mock_run.side_effect = [_not_found_result(), _active_result()]
        assert _rw._is_unit_enabled_or_active("sreda-telegram-poller@sreda.service") is True


def test_is_unit_enabled_returns_true():
    """Unit that is enabled → True (checked before is-active)."""
    with patch("sreda.scripts.restore_webhook.subprocess.run") as mock_run:
        mock_run.side_effect = [_enabled_result()]
        assert _rw._is_unit_enabled_or_active("sreda-telegram-poller@sreda.service") is True


def test_is_unit_not_found_returns_false():
    """Unit that doesn't exist → False for both is-enabled and is-active."""
    with patch("sreda.scripts.restore_webhook.subprocess.run") as mock_run:
        mock_run.side_effect = [_not_found_result(), _not_found_result()]
        assert _rw._is_unit_enabled_or_active("sreda-telegram-poller@sreda.service") is False


def test_is_unit_inactive_and_disabled_returns_false():
    """Unit that exists but is inactive/disabled → False."""
    with patch("sreda.scripts.restore_webhook.subprocess.run") as mock_run:
        # is-enabled returns disabled (returncode=1, stdout="disabled")
        disabled = _make_systemctl_result(stdout="disabled\n", returncode=1)
        mock_run.side_effect = [disabled, _inactive_result()]
        assert _rw._is_unit_enabled_or_active("sreda-telegram-poller@sreda.service") is False


# ---------------------------------------------------------------------------
# _any_poller_active
# ---------------------------------------------------------------------------

def test_any_poller_active_returns_active_units():
    """Returns the list of active units."""
    def fake_systemctl(args, **kwargs):
        unit = args[2]  # systemctl is-enabled/is-active <unit>
        sub = args[1]
        if unit == "sreda-telegram-poller@sreda.service" and sub == "is-enabled":
            return _enabled_result()
        return _not_found_result()

    with patch("sreda.scripts.restore_webhook.subprocess.run", side_effect=fake_systemctl):
        active = _rw._any_poller_active()
    assert "sreda-telegram-poller@sreda.service" in active


def test_any_poller_active_returns_empty_when_none_active():
    """Returns empty list when no poller unit is enabled or active."""
    with patch("sreda.scripts.restore_webhook.subprocess.run") as mock_run:
        mock_run.return_value = _not_found_result()
        active = _rw._any_poller_active()
    assert active == []


# ---------------------------------------------------------------------------
# main() guard
# ---------------------------------------------------------------------------

def _patch_active_pollers(active: list[str]):
    """Patch _any_poller_active to return a given list."""
    return patch.object(_rw, "_any_poller_active", return_value=active)


def test_main_refuses_when_poller_active(capsys):
    """main() returns 1 and prints error when a poller unit is active."""
    with _patch_active_pollers(["sreda-telegram-poller@sreda.service"]):
        with patch("sys.argv", ["restore_webhook"]):  # no --force-webhook-mode
            rc = _rw.main()

    assert rc == 1
    captured = capsys.readouterr()
    assert "sreda-telegram-poller@sreda.service" in captured.err
    assert "--force-webhook-mode" in captured.err


def test_main_proceeds_when_no_poller_active(monkeypatch, capsys):
    """main() proceeds to settings validation when no poller is active."""
    with _patch_active_pollers([]):
        with patch("sys.argv", ["restore_webhook"]):
            # Settings will be missing token — that's the next error, not the guard.
            with patch.object(_rw, "get_settings") as mock_settings:
                mock_settings.return_value.telegram_bot_token = None
                mock_settings.return_value.telegram_webhook_secret_token = None
                rc = _rw.main()

    # rc=1 from missing token (guard passed, settings validation failed)
    assert rc == 1
    captured = capsys.readouterr()
    assert "SREDA_TELEGRAM_BOT_TOKEN" in captured.err
    # Must NOT contain the poller-active error message
    assert "sreda-telegram-poller" not in captured.err


def test_main_with_force_webhook_mode_bypasses_guard(capsys):
    """--force-webhook-mode bypasses the guard but still validates settings."""
    with _patch_active_pollers(["sreda-telegram-poller@sreda.service"]):
        with patch("sys.argv", ["restore_webhook", "--force-webhook-mode"]):
            with patch.object(_rw, "get_settings") as mock_settings:
                mock_settings.return_value.telegram_bot_token = None
                mock_settings.return_value.telegram_webhook_secret_token = None
                rc = _rw.main()

    # rc=1 from missing token — guard was bypassed, reached settings validation
    assert rc == 1
    captured = capsys.readouterr()
    assert "SREDA_TELEGRAM_BOT_TOKEN" in captured.err
