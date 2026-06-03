"""Telegram Mini App initData validation.

Implements the server-side verification of ``Telegram.WebApp.initData``
as specified in https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

The ``hash`` field is an HMAC-SHA256 signature derived from the bot token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from sreda.config.bot_registry import TelegramBotRegistry


class TelegramInitDataError(Exception):
    """Raised when initData validation fails (bad signature, expired, etc.)."""


@dataclass(slots=True)
class TelegramWebAppUser:
    telegram_id: str
    first_name: str | None
    username: str | None
    start_param: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers (shared between single-bot and multi-bot paths)
# ---------------------------------------------------------------------------

def _parse_init_data(
    init_data_raw: str,
) -> tuple[str, list[tuple[str, str]], str]:
    """Parse raw initData string.

    Returns ``(received_hash, sorted_data_pairs, data_check_string)``.
    Raises ``TelegramInitDataError`` on structural errors.
    """
    if not init_data_raw:
        raise TelegramInitDataError("empty initData")

    pairs = parse_qsl(init_data_raw, keep_blank_values=True)
    if not pairs:
        raise TelegramInitDataError("initData contains no parameters")

    received_hash: str | None = None
    data_pairs: list[tuple[str, str]] = []
    for key, value in pairs:
        if key == "hash":
            received_hash = value
        else:
            data_pairs.append((key, value))

    if not received_hash:
        raise TelegramInitDataError("missing hash parameter")

    data_pairs.sort(key=lambda p: p[0])
    data_check_string = "\n".join(f"{k}={v}" for k, v in data_pairs)
    return received_hash, data_pairs, data_check_string


def _compute_hmac(bot_token: str, data_check_string: str) -> str:
    """Compute the HMAC-SHA256 hash for *data_check_string* using *bot_token*."""
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    return hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _extract_user(
    data_pairs: list[tuple[str, str]],
    *,
    max_age_seconds: int,
) -> TelegramWebAppUser:
    """Extract and validate auth_date + user fields from *data_pairs*.

    Raises ``TelegramInitDataError`` on expiry or missing/invalid fields.
    """
    auth_date_str: str | None = None
    user_json: str | None = None
    start_param: str | None = None
    for key, value in data_pairs:
        if key == "auth_date":
            auth_date_str = value
        elif key == "user":
            user_json = value
        elif key == "start_param":
            start_param = value

    if auth_date_str is None:
        raise TelegramInitDataError("missing auth_date")

    try:
        auth_date = int(auth_date_str)
    except ValueError as exc:
        raise TelegramInitDataError("invalid auth_date") from exc

    age = time.time() - auth_date
    if age > max_age_seconds:
        raise TelegramInitDataError(
            f"initData expired ({int(age)}s > {max_age_seconds}s)"
        )

    if not user_json:
        raise TelegramInitDataError("missing user field")

    try:
        user_data = json.loads(user_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise TelegramInitDataError("invalid user JSON") from exc

    user_id = user_data.get("id")
    if user_id is None:
        raise TelegramInitDataError("user.id missing")

    return TelegramWebAppUser(
        telegram_id=str(user_id),
        first_name=user_data.get("first_name"),
        username=user_data.get("username"),
        start_param=start_param,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate_init_data(
    init_data_raw: str,
    bot_token: str,
    *,
    max_age_seconds: int = 86400,  # 24h, indust standard
) -> TelegramWebAppUser:
    """Parse and validate Telegram Mini App ``initData``.

    Returns a ``TelegramWebAppUser`` on success.
    Raises ``TelegramInitDataError`` on any validation failure.
    """
    received_hash, data_pairs, data_check_string = _parse_init_data(init_data_raw)
    computed_hash = _compute_hmac(bot_token, data_check_string)

    if not hmac.compare_digest(computed_hash, received_hash):
        raise TelegramInitDataError("invalid hash")

    return _extract_user(data_pairs, max_age_seconds=max_age_seconds)


# Maximum number of registered bots we will try when validating initData.
# Keeps the per-request work O(1) in practice and prevents abuse if the
# registry ever grows unexpectedly.
_MAX_MINIAPP_BOTS = 8


def validate_telegram_init_data_any_bot(
    init_data_raw: str,
    registry: TelegramBotRegistry,
    *,
    max_age_seconds: int = 86400,
) -> tuple[str, TelegramWebAppUser]:
    """Validate Telegram Mini App ``initData`` against all registered bots.

    Telegram signs initData with the token of the bot from which the Mini App
    was opened, so a single-token check fails when the user opens the Mini App
    from a second bot.  This function tries every bot whose ``miniapp_shortname``
    is set (i.e. every bot that has a Mini App), returns on the first match, and
    provides the matched ``bot_key`` to the caller.

    Security properties
    -------------------
    * The initData string is **parsed once** (structural errors raise immediately).
    * The HMAC is **computed for every bot** in the candidate list — including
      tokens after a match is found — so the total compute time is independent
      of which bot matched (no early-return timing oracle).
    * Comparison uses ``hmac.compare_digest`` for constant-time equality.
    * The candidate list is capped at ``_MAX_MINIAPP_BOTS`` (currently 8).
    * On zero matches the generic ``TelegramInitDataError("invalid hash")`` is
      raised — identical to the single-bot path; no information about which
      token was tried is leaked.

    Parameters
    ----------
    init_data_raw:
        Raw ``Authorization: TMA <value>`` payload from the client.
    registry:
        ``TelegramBotRegistry`` instance.  All bots with a non-None
        ``miniapp_shortname`` are candidates.  Falls back to ALL bots if none
        have ``miniapp_shortname`` set (forward-compat for single-bot setups
        where ``miniapp_shortname`` may be ``None`` but the bot is still used).
    max_age_seconds:
        Maximum allowed age of ``auth_date`` (default 24 h).

    Returns
    -------
    ``(bot_key, TelegramWebAppUser)`` for the bot whose token signed this
    initData.

    Raises
    ------
    TelegramInitDataError
        On structural errors (empty/missing fields) or when no registered
        bot's token matches the HMAC.
    """
    # 1. Parse once — structural errors surface here before any HMAC work.
    received_hash, data_pairs, data_check_string = _parse_init_data(init_data_raw)

    # 2. Build candidate list: prefer bots with a Mini App; fallback to all.
    #    SECURITY: exclude bots with an empty/missing token — an empty token
    #    produces a forgeable HMAC (key="") that any attacker can replicate.
    all_bots = [b for b in registry.all_bots() if b.token]
    if not all_bots:
        raise TelegramInitDataError(
            "No bots with a non-empty token are configured; cannot validate initData."
        )
    # AUTH must HMAC-check ALL non-empty-token bots: initData can be signed by
    # ANY registered bot's token. ``miniapp_shortname`` is only for LINK
    # generation — filtering auth by it would break a token-valid bot whose
    # shortname is unset while another bot has one (Codex R2 high).
    candidates = all_bots[:_MAX_MINIAPP_BOTS]

    # 3. Compute HMAC for ALL candidates (no early exit on match) to ensure
    #    the wall-clock time is the same regardless of which bot matched.
    matched_key: str | None = None
    for bot in candidates:
        computed = _compute_hmac(bot.token, data_check_string)
        # constant-time compare; do NOT break early
        if hmac.compare_digest(computed, received_hash):
            if matched_key is None:
                matched_key = bot.key

    # 4. Generic rejection — do not reveal which token was tried.
    if matched_key is None:
        raise TelegramInitDataError("invalid hash")

    # 5. Freshness + user extraction (only after we know the hash is valid).
    user = _extract_user(data_pairs, max_age_seconds=max_age_seconds)
    return matched_key, user


def resolve_tenant_from_telegram_id(
    session: Session, telegram_id: str
) -> tuple[str, str] | None:
    """Look up tenant_id and user_id by Telegram account ID.

    Returns ``(tenant_id, user_id)`` or ``None`` if no matching user.
    """
    # 152-ФЗ обезличивание Часть 1: lookup идёт через hash, не через
    # plaintext (см. services.tg_account_hash + миграция 0027).
    from sreda.services.onboarding import find_user_by_chat_id

    user = find_user_by_chat_id(session, telegram_id)
    if user is None:
        return None
    return user.tenant_id, user.id
