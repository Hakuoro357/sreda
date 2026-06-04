"""CI guard: no hardcoded-token TelegramClient construction outside the allowlist.

Phase 4b enforcement test (plans/second-tg-bot-final.md).

Scans ``src/sreda/`` Python sources and FAILS if it finds, outside an
explicit allowlist of files:

1. ``TelegramClient(`` constructed with a raw token from settings
   (i.e. the argument contains ``telegram_bot_token`` or
   ``bot_token`` without going through ``telegram_client_for``).
2. Raw ``os.environ`` / ``getenv`` reads of the Telegram token env vars
   (``SREDA_TELEGRAM_BOT_TOKEN``, ``SREDA_HOME_BOT_TOKEN``).
3. Direct Telegram Bot API HTTP calls — the literal string
   ``https://api.telegram.org/bot`` outside the approved TelegramClient
   module and the admin_alerts sync helper (which uses raw httpx by
   design in a thread context).

Allowlist (each entry must be justified):

* ``integrations/telegram/client.py``
  The single approved TelegramClient implementation.  All token usage
  is internal here.

* ``config/bot_registry.py``
  The approved factory (``telegram_client_for``).  Holds the token
  *per-BotConfig* only to pass it to TelegramClient.

* ``services/admin_alerts.py``
  Uses raw ``httpx.post`` to ``api.telegram.org`` in a sync background
  thread.  async ``TelegramClient`` cannot be used there.  Token is now
  resolved via ``registry.admin_bot_key``, not hardcoded from settings.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Callable

import pytest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_SRC_ROOT = Path(__file__).parent.parent.parent / "src" / "sreda"

# Top-level scripts directory (broadcast / maintenance scripts).
# Separate scan root with its own allowlist so any future raw-token
# regression in a new script is caught.
_SCRIPTS_ROOT = Path(__file__).parent.parent.parent / "scripts"

# Relative to _SRC_ROOT.  Files here are exempt from all three checks.
_ALLOWLIST: set[str] = {
    # The single approved TelegramClient implementation.
    "integrations/telegram/client.py",
    # The approved factory (telegram_client_for); holds per-BotConfig tokens.
    "config/bot_registry.py",
    # Operator alerts via raw httpx.post in a sync thread; token now resolved
    # via registry.admin_bot_key, not hardcoded.
    "services/admin_alerts.py",
    # Long-poll infrastructure: calls Telegram getUpdates directly (low-level
    # worker, analogous to the client module); per-bot token from the registry.
    "workers/telegram_long_poll.py",
    # Webhook-management operational script. Phase 8 gates/archives it
    # (--force-webhook-mode + fail-if-poller-active). TODO: re-evaluate at Phase 8.
    "scripts/restore_webhook.py",
}

# Allowlist for top-level scripts/ (relative to _SCRIPTS_ROOT).
# broadcast_welcome_v2.py uses telegram_client_for — no raw token construction.
_SCRIPTS_ALLOWLIST: set[str] = {
    # Uses telegram_client_for(bot_key, registry) — approved factory pattern.
    "broadcast_welcome_v2.py",
    # Health/latency monitoring script. Uses raw httpx.get/post to
    # api.telegram.org/bot for getMe/getWebhookInfo/sendMessage probes —
    # the same rationale as admin_alerts.py in the src/sreda allowlist
    # (sync/monitoring context where TelegramClient cannot be used).
    "monitor_health.py",
}

# Allowlist for top-level scripts/*.sh (relative to _SCRIPTS_ROOT).
_SCRIPTS_SH_ALLOWLIST: set[str] = {
    # Long-poll restart: legitimately calls api.telegram.org deleteWebhook /
    # getWebhookInfo per bot (webhook-management; no token send to users).
    "safe_restart.sh",
}

# Pattern: TelegramClient( followed by anything containing a raw token
# expression (settings.telegram_bot_token / settings.home_bot_token /
# bot_token=... etc.) without going through telegram_client_for.
#
# We use a simple heuristic: if the source of a file (excluding the
# allowlist) contains a TelegramClient( call AND the call passes a token
# that comes from settings or os.environ directly, that's a violation.
#
# The structural approach: parse each .py file and look for
#   Call(func=Name/Attr("TelegramClient"), args=[<token-bearing-arg>])
# where a token-bearing arg is any expression whose source text contains
# "telegram_bot_token" or "bot_token" (the raw settings attribute),
# NOT preceded by telegram_client_for.
_RAW_TOKEN_ATTRS = re.compile(r"\btelegram_bot_token\b|\bhome_bot_token\b")
_TELEGRAM_API_URL = "https://api.telegram.org/bot"
_ENV_VAR_PATTERN = re.compile(
    r'SREDA_TELEGRAM_BOT_TOKEN|SREDA_HOME_BOT_TOKEN'
)


def _relative(path: Path) -> str:
    """Return POSIX-style path relative to _SRC_ROOT."""
    return path.relative_to(_SRC_ROOT).as_posix()


def _is_allowlisted(path: Path) -> bool:
    return _relative(path) in _ALLOWLIST


def _iter_py_files():
    for f in _SRC_ROOT.rglob("*.py"):
        if not _is_allowlisted(f):
            yield f


def _scripts_relative(path: Path) -> str:
    """Return POSIX-style path relative to _SCRIPTS_ROOT."""
    return path.relative_to(_SCRIPTS_ROOT).as_posix()


def _is_scripts_allowlisted(path: Path) -> bool:
    return _scripts_relative(path) in _SCRIPTS_ALLOWLIST


def _iter_scripts_py_files():
    """Yield non-allowlisted .py files from the top-level scripts/ directory."""
    if not _SCRIPTS_ROOT.is_dir():
        return
    for f in _SCRIPTS_ROOT.rglob("*.py"):
        if not _is_scripts_allowlisted(f):
            yield f


def _iter_scripts_sh_files():
    """Yield non-allowlisted .sh files from the top-level scripts/ directory."""
    if not _SCRIPTS_ROOT.is_dir():
        return
    for f in _SCRIPTS_ROOT.rglob("*.sh"):
        if _scripts_relative(f) not in _SCRIPTS_SH_ALLOWLIST:
            yield f


# ---------------------------------------------------------------------------
# Check 1: TelegramClient( constructed with raw settings token
# ---------------------------------------------------------------------------

def _find_raw_telegram_client_calls(
    source: str,
    filepath: Path,
    rel_fn: Callable[[Path], str] = _relative,
) -> list[str]:
    """Return violation messages for TelegramClient calls with raw token args."""
    violations: list[str] = []
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return violations

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Check if the callable is TelegramClient
        func = node.func
        is_tg_client = (
            (isinstance(func, ast.Name) and func.id == "TelegramClient")
            or (isinstance(func, ast.Attribute) and func.attr == "TelegramClient")
        )
        if not is_tg_client:
            continue

        # Collect all arg/kwarg source text
        all_args = list(node.args) + [kw.value for kw in node.keywords]
        for arg in all_args:
            arg_src = ast.get_source_segment(source, arg) or ""
            if _RAW_TOKEN_ATTRS.search(arg_src):
                violations.append(
                    f"{rel_fn(filepath)}:{node.lineno}: "
                    f"TelegramClient( called with raw token arg: {arg_src!r}. "
                    f"Use telegram_client_for(bot_key, registry) instead."
                )
    return violations


# ---------------------------------------------------------------------------
# Check 2: raw os.environ / getenv reads for token env vars
# ---------------------------------------------------------------------------

def _find_raw_env_token_reads(
    source: str,
    filepath: Path,
    rel_fn: Callable[[Path], str] = _relative,
) -> list[str]:
    """Return violations for raw os.environ reads of token env vars."""
    violations: list[str] = []
    for i, line in enumerate(source.splitlines(), start=1):
        if _ENV_VAR_PATTERN.search(line):
            # Allow lines that are comments or inside string literals
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Allow env var names that appear in .env.example / settings definitions
            # (i.e. the string itself is a key reference, not an access).
            # Heuristic: violation only if os.environ[ or getenv( also present.
            if "os.environ" in line or "getenv(" in line:
                violations.append(
                    f"{rel_fn(filepath)}:{i}: "
                    f"Raw os.environ/getenv read of Telegram token env var. "
                    f"Use TelegramBotRegistry.from_settings(get_settings()) instead."
                )
    return violations


# ---------------------------------------------------------------------------
# Check 3: direct api.telegram.org URL
# ---------------------------------------------------------------------------

def _find_raw_tg_api_urls(
    source: str,
    filepath: Path,
    rel_fn: Callable[[Path], str] = _relative,
) -> list[str]:
    """Return violations for raw api.telegram.org/bot URL construction."""
    violations: list[str] = []
    for i, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if _TELEGRAM_API_URL in line:
            violations.append(
                f"{rel_fn(filepath)}:{i}: "
                f"Direct Telegram Bot API URL outside approved client module. "
                f"Use TelegramClient (from integrations/telegram/client.py) instead."
            )
    return violations


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

def _collect_violations(
    iter_files,
    *checkers,
) -> list[str]:
    """Run all checker functions over all files from iter_files."""
    violations: list[str] = []
    for pyfile in iter_files():
        try:
            source = pyfile.read_text(encoding="utf-8")
        except OSError:
            continue
        for checker in checkers:
            violations.extend(checker(source, pyfile))
    return violations


def test_no_hardcoded_telegram_token_sends():
    """Fail if any non-allowlisted file builds TelegramClient from raw token.

    Scans both src/sreda/ and the top-level scripts/ directory.
    """
    all_violations: list[str] = []
    for pyfile in _iter_py_files():
        try:
            source = pyfile.read_text(encoding="utf-8")
        except OSError:
            continue
        all_violations.extend(_find_raw_telegram_client_calls(source, pyfile))
    for pyfile in _iter_scripts_py_files():
        try:
            source = pyfile.read_text(encoding="utf-8")
        except OSError:
            continue
        all_violations.extend(_find_raw_telegram_client_calls(source, pyfile, rel_fn=_scripts_relative))

    if all_violations:
        msg = (
            "Hardcoded-token TelegramClient calls found outside the allowlist.\n"
            "Use telegram_client_for(bot_key, registry) instead.\n\n"
            + "\n".join(all_violations)
        )
        pytest.fail(msg)


def test_no_raw_token_env_reads():
    """Fail if any non-allowlisted file reads raw Telegram token env vars.

    Scans both src/sreda/ and the top-level scripts/ directory.
    """
    all_violations: list[str] = []
    for pyfile in _iter_py_files():
        try:
            source = pyfile.read_text(encoding="utf-8")
        except OSError:
            continue
        all_violations.extend(_find_raw_env_token_reads(source, pyfile))
    for pyfile in _iter_scripts_py_files():
        try:
            source = pyfile.read_text(encoding="utf-8")
        except OSError:
            continue
        all_violations.extend(_find_raw_env_token_reads(source, pyfile, rel_fn=_scripts_relative))

    if all_violations:
        msg = (
            "Raw os.environ/getenv reads for Telegram token env vars found "
            "outside the allowlist.\n"
            "Use TelegramBotRegistry.from_settings(get_settings()) instead.\n\n"
            + "\n".join(all_violations)
        )
        pytest.fail(msg)


def test_no_raw_telegram_api_urls():
    """Fail if any non-allowlisted file contains a raw api.telegram.org/bot URL.

    Scans both src/sreda/ and the top-level scripts/ directory.
    """
    all_violations: list[str] = []
    for pyfile in _iter_py_files():
        try:
            source = pyfile.read_text(encoding="utf-8")
        except OSError:
            continue
        all_violations.extend(_find_raw_tg_api_urls(source, pyfile))
    for pyfile in _iter_scripts_py_files():
        try:
            source = pyfile.read_text(encoding="utf-8")
        except OSError:
            continue
        all_violations.extend(_find_raw_tg_api_urls(source, pyfile, rel_fn=_scripts_relative))
    for shfile in _iter_scripts_sh_files():
        try:
            source = shfile.read_text(encoding="utf-8")
        except OSError:
            continue
        all_violations.extend(_find_raw_tg_api_urls(source, shfile, rel_fn=_scripts_relative))

    if all_violations:
        msg = (
            "Direct Telegram Bot API URL found outside the allowlist.\n"
            "Use TelegramClient from integrations/telegram/client.py instead.\n\n"
            + "\n".join(all_violations)
        )
        pytest.fail(msg)
