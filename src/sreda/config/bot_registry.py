"""Bot registry for the Sreda multi-bot architecture.

Phase 1 of the second-Telegram-bot feature (plans/second-tg-bot-final.md).

Provides:
  - ``BotConfig``          — per-bot configuration dataclass
  - ``TelegramBotRegistry``— registry of all configured bots; fail-closed on
                              unknown bot_key
  - ``telegram_client_for``— single factory: bot_key → ``TelegramClient``
  - ``verify_bot_configs`` — fail-closed pre-start check: getMe per bot +
                              username match + token uniqueness
  - Three canonical key constants used across the codebase:
      ``LEGACY_NULL_BOT_KEY``     — fallback for NULL bot_key during migration
      ``system_default_bot_key``  — property on registry; env-driven
      ``admin_bot_key``           — property on registry; env-driven
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Awaitable, Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical key constants
# ---------------------------------------------------------------------------

#: Fallback bot_key for inbound rows that have NULL bot_key during the
#: migration window (Phase 4 backfill). Must never change after initial deploy.
LEGACY_NULL_BOT_KEY: str = "sreda"


# ---------------------------------------------------------------------------
# BotConfig
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BotConfig:
    """Immutable configuration for a single Telegram bot.

    Fields
    ------
    key
        Short alphanumeric identifier used throughout the codebase
        (e.g. ``"sreda"``, ``"sreda_home"``).
    token
        Telegram Bot API token.  Never logged or repr'd — the registry
        masks it.  Callers retrieve it via ``BotConfig.token`` only when
        passing it to ``TelegramClient``.
    username
        Bot username WITHOUT leading ``@`` (e.g. ``"SredaBot"``).
        Used by ``verify_bot_configs`` to assert token↔bot mapping via
        ``getMe``.
    miniapp_shortname
        Telegram Mini App short-name for this bot's launch link.  ``None``
        if the bot has no Mini App.
    signup_open
        When ``True`` the bot accepts new users without waiting for manual
        tenant moderation approval (anti-abuse still applies).
    inbound_mode
        How inbound updates arrive.  Always ``"long_poll"`` in production;
        ``"webhook"`` path is archived (2026-04-30 incident).
    """

    key: str
    token: str
    username: str | None = None
    miniapp_shortname: str | None = None
    signup_open: bool = True
    inbound_mode: str = "long_poll"

    def __repr__(self) -> str:
        # Never expose token in repr — same pattern as Settings._SECRET_FIELD_NAMES.
        return (
            f"BotConfig(key={self.key!r}, token='***', "
            f"username={self.username!r}, "
            f"miniapp_shortname={self.miniapp_shortname!r}, "
            f"signup_open={self.signup_open!r}, "
            f"inbound_mode={self.inbound_mode!r})"
        )


# ---------------------------------------------------------------------------
# TelegramBotRegistry
# ---------------------------------------------------------------------------

class TelegramBotRegistry:
    """Registry of all configured Telegram bots.

    Build via ``TelegramBotRegistry.from_settings(settings)`` at startup.
    All lookups are fail-closed: unknown ``bot_key`` raises ``KeyError``
    with a descriptive message rather than returning ``None``.
    """

    def __init__(
        self,
        bots: list[BotConfig],
        *,
        system_default_bot_key: str = LEGACY_NULL_BOT_KEY,
        admin_bot_key: str = LEGACY_NULL_BOT_KEY,
    ) -> None:
        if not bots:
            raise ValueError("TelegramBotRegistry requires at least one BotConfig")
        self._bots: dict[str, BotConfig] = {b.key: b for b in bots}
        if len(self._bots) != len(bots):
            keys = [b.key for b in bots]
            duplicates = [k for k in keys if keys.count(k) > 1]
            raise ValueError(
                f"TelegramBotRegistry: duplicate bot keys: {sorted(set(duplicates))}"
            )
        # Fail-closed: the routing keys MUST reference real registered bots.
        # Otherwise a typo or a premature primary-flip (e.g.
        # SREDA_SYSTEM_DEFAULT_BOT_KEY=sreda_home before that bot is configured)
        # would crash job_runner / broadcast / admin-alerts at first resolve,
        # and --check-config would not catch it (Codex R2 high).
        for _label, _key in (
            ("system_default_bot_key", system_default_bot_key),
            ("admin_bot_key", admin_bot_key),
        ):
            if _key not in self._bots:
                raise ValueError(
                    f"TelegramBotRegistry: {_label}={_key!r} is not a registered "
                    f"bot. Known keys: {sorted(self._bots)}."
                )
        self._system_default_bot_key = system_default_bot_key
        self._admin_bot_key = admin_bot_key

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def system_default_bot_key(self) -> str:
        """Bot used for system broadcasts and outbox rows with no explicit bot_key."""
        return self._system_default_bot_key

    @property
    def admin_bot_key(self) -> str:
        """Bot used for admin alerts sent to the operator."""
        return self._admin_bot_key

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def resolve(self, bot_key: str) -> BotConfig:
        """Return ``BotConfig`` for *bot_key*.

        Raises
        ------
        KeyError
            When *bot_key* is not in the registry.  Fail-closed: callers
            must not pass unknown keys.
        """
        try:
            return self._bots[bot_key]
        except KeyError:
            known = sorted(self._bots)
            raise KeyError(
                f"Unknown bot_key {bot_key!r}. "
                f"Known keys: {known}. "
                f"Check SREDA_HOME_BOT_TOKEN / SREDA_HOME_BOT_USERNAME env vars."
            ) from None

    def all_bots(self) -> list[BotConfig]:
        """All registered bots, in insertion order."""
        return list(self._bots.values())

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Any) -> "TelegramBotRegistry":
        """Build registry from a ``Settings`` instance.

        The ``"sreda"`` bot is always present (backward-compat) and uses
        the existing ``telegram_*`` config fields.  The ``"sreda_home"``
        bot is added only when ``home_bot_token`` is set.

        Empty primary token (``telegram_bot_token=None`` — MAX-only/dev
        config): ``"sreda"`` stays registered so routing-key validation and
        ``resolve()`` keep working for channel bookkeeping, but
        ``telegram_client_for`` refuses to build a client for it
        (fail-closed — see there). A warning is logged here so the
        misconfiguration is visible instead of surfacing later as
        confusing Telegram 404s.
        """
        bots: list[BotConfig] = []

        # --- Primary / legacy bot (always "sreda") ----------------------
        if not settings.telegram_bot_token:
            logger.warning(
                "telegram_bot_token is empty — bot 'sreda' registered without "
                "a token; telegram_client_for('sreda') will raise (fail-closed)"
            )
        bots.append(BotConfig(
            key="sreda",
            token=settings.telegram_bot_token or "",
            username=settings.telegram_bot_username,
            miniapp_shortname=settings.telegram_miniapp_shortname,
            # Legacy bot: signup_open defaults to True (existing behaviour).
            signup_open=True,
            inbound_mode="long_poll",
        ))

        # --- Second bot (sreda_home) — only when configured --------------
        if settings.home_bot_token:
            bots.append(BotConfig(
                key="sreda_home",
                token=settings.home_bot_token,
                username=settings.home_bot_username,
                miniapp_shortname=settings.home_miniapp_shortname,
                signup_open=settings.home_bot_signup_open,
                inbound_mode="long_poll",
            ))

        return cls(
            bots,
            system_default_bot_key=settings.system_default_bot_key,
            admin_bot_key=settings.admin_bot_key,
        )


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------

def telegram_client_for(bot_key: str, registry: TelegramBotRegistry) -> "Any":
    """Return a ``TelegramClient`` for the given *bot_key*.

    This is the single approved factory for creating Telegram clients.
    Phase 4 will route all call-sites through here; for now it exists so
    new code can use it without importing the raw token.

    Raises
    ------
    KeyError
        Propagated from ``registry.resolve`` when *bot_key* is unknown.
    ValueError
        When the resolved bot has an empty token (fail-closed, see below).
    """
    # Import here to avoid a circular import at module load time
    # (client.py → bot_registry would be fine, but registry.py → client.py
    # would create a cycle if client.py ever imports from config).
    from sreda.integrations.telegram.client import TelegramClient

    cfg = registry.resolve(bot_key)
    # Fail-closed on empty token (2026-07-18 audit, config-integ MINOR):
    # previously a bot registered with token="" produced TelegramClient("")
    # whose every call 404-looped as confusing
    # ``TelegramDeliveryError non-retryable`` on each background send
    # (outbox / admin routing). Raise a descriptive error at the factory
    # instead — same fail-closed policy as the long-poll entrypoint, which
    # refuses to start on an empty token.
    if not cfg.token:
        known = [b.key for b in registry.all_bots()]
        raise ValueError(
            f"Bot {bot_key!r} has an empty token (telegram_bot_token not set?). "
            f"Refusing to build TelegramClient. "
            f"Configure the token or use another bot_key. Known keys: {known}."
        )
    return TelegramClient(cfg.token)


# ---------------------------------------------------------------------------
# Fail-closed verification
# ---------------------------------------------------------------------------

#: Type alias for the injectable getMe callable used in tests and production.
#: Signature: (token: str) -> Awaitable[dict]  — returns parsed getMe result.
GetMeFn = Callable[[str], Awaitable[dict]]


async def verify_bot_configs(
    registry: TelegramBotRegistry,
    *,
    get_me: GetMeFn,
) -> None:
    """Fail-closed pre-start verification of all registered bots.

    Checks:
    1. Token uniqueness — two bots with the same token would create a
       getUpdates 409 conflict and silently mis-route inbound messages.
    2. For each bot whose ``username`` is set: calls ``get_me(token)``
       and asserts the returned ``username`` matches ``BotConfig.username``
       (case-insensitive).  A swapped token (wrong env var) would pass the
       409 check but silently bind the wrong logical bot to inbound traffic.

    Parameters
    ----------
    registry:
        Registry to verify.
    get_me:
        Async callable ``(token: str) -> dict`` that returns the raw Telegram
        ``getMe`` result (the ``result`` sub-dict).  Inject a stub in tests;
        the real call is wired in the Phase 3 ``--check-config`` CLI path.

    Raises
    ------
    ValueError
        On token duplicate or username mismatch.  Always raised before any
        poller starts so a misconfigured env never silently serves traffic.
    """
    bots = registry.all_bots()

    # --- 1. Token uniqueness --------------------------------------------
    tokens = [b.token for b in bots if b.token]
    if len(tokens) != len(set(tokens)):
        seen: set[str] = set()
        duplicates: list[str] = []
        for b in bots:
            if b.token in seen:
                duplicates.append(b.key)
            seen.add(b.token)
        raise ValueError(
            f"Bot registry has duplicate tokens for keys: {duplicates}. "
            f"Each bot must have a unique token.  Check env var assignments."
        )

    # --- 2. Username match via getMe ------------------------------------
    for bot in bots:
        if not bot.username:
            # Username not configured — skip getMe check for this bot.
            continue
        result = await get_me(bot.token)
        actual_username = (result.get("username") or "").lower()
        expected_username = bot.username.lower()
        if actual_username != expected_username:
            raise ValueError(
                f"Bot key {bot.key!r}: getMe returned username "
                f"{actual_username!r} but BotConfig.username is "
                f"{expected_username!r}.  Tokens are likely swapped in env."
            )
