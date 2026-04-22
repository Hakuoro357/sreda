"""Fetch remaining credits / rate-limit budgets for chat + STT
providers and render them for the /admin/llm dashboard.

Each ``_fetch_<provider>`` routine is defensive: any exception is
swallowed into a ``status="error"`` row so one outage can't hide the
other providers. Results are cached for 60 seconds in-process so an
admin page refresh doesn't hammer the billing endpoints.

Outbound proxy honouring: Groq from a Russian IP goes through the
pproxy → VDS tunnel same as our speech call path; the helper reads
the same env vars ``services.speech.groq`` does to stay consistent.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from sreda.config.settings import Settings

logger = logging.getLogger(__name__)


_CACHE_TTL = 60.0
_cache_lock = threading.Lock()
_cache: dict[str, tuple[list["ProviderBalance"], float]] = {}


@dataclass(slots=True)
class ProviderBalance:
    key: str            # matches the internal provider id (mimo / openrouter / groq / ...)
    label: str          # human-readable in the UI
    status: str         # "ok" | "error" | "not_configured" | "not_supported"
    headline: str       # one-line balance string, e.g. "$9.847 из $10.000"
    details: str = ""   # optional 2nd line (rate-limit usage, period)


def _outbound_proxy() -> str | None:
    for var in ("SREDA_GROQ_HTTP_PROXY", "HTTPS_PROXY", "HTTP_PROXY",
                "https_proxy", "http_proxy"):
        value = os.environ.get(var)
        if value:
            return value
    return None


def _http_client_kwargs(*, use_proxy: bool) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"timeout": 10.0}
    if use_proxy:
        proxy = _outbound_proxy()
        if proxy:
            kwargs["proxy"] = proxy
    return kwargs


# ---------------------------------------------------------------------------
# Per-provider fetchers
# ---------------------------------------------------------------------------


def _fetch_openrouter(settings: Settings) -> ProviderBalance:
    """OpenRouter exposes a clean /api/v1/key endpoint that returns
    total spend + limit in dollars. We call it through the pproxy
    tunnel because prod Mac sits behind a Russian IP and OpenRouter
    occasionally geo-blocks; dev machines without the proxy just go
    direct."""
    key = settings.resolve_openrouter_api_key()
    if not key:
        return ProviderBalance(
            key="openrouter", label="OpenRouter",
            status="not_configured", headline="ключ не настроен",
        )
    try:
        with httpx.Client(**_http_client_kwargs(use_proxy=True)) as client:
            resp = client.get(
                f"{settings.openrouter_base_url}/key",
                headers={"Authorization": f"Bearer {key}"},
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("provider_balances: OpenRouter fetch failed: %s", exc)
        return ProviderBalance(
            key="openrouter", label="OpenRouter",
            status="error", headline="ошибка запроса",
            details=f"{type(exc).__name__}: {str(exc)[:80]}",
        )
    usage = float(data.get("usage") or 0.0)
    limit = data.get("limit")  # None → unlimited
    daily = float(data.get("usage_daily") or 0.0)
    remaining = data.get("limit_remaining")
    if limit is None:
        headline = f"потрачено ${usage:.4f} (лимит не задан)"
    else:
        limit_f = float(limit)
        remaining_f = (
            float(remaining) if remaining is not None else max(limit_f - usage, 0.0)
        )
        headline = f"${remaining_f:.4f} из ${limit_f:.4f} (потрачено ${usage:.4f})"
    details = f"за сегодня: ${daily:.4f}"
    return ProviderBalance(
        key="openrouter", label="OpenRouter",
        status="ok", headline=headline, details=details,
    )


def _read_rate_limit_headers(headers: httpx.Headers) -> str:
    """Extract rate-limit summary from OpenAI-compatible response
    headers. Covers Groq + Cerebras shapes: both publish
    ``x-ratelimit-remaining-*`` and ``x-ratelimit-limit-*``."""
    pairs: list[str] = []
    for kind in ("requests", "tokens"):
        remaining = headers.get(f"x-ratelimit-remaining-{kind}")
        limit = headers.get(f"x-ratelimit-limit-{kind}")
        if remaining and limit:
            pairs.append(f"{kind}: {remaining}/{limit}")
        elif remaining:
            pairs.append(f"{kind}: {remaining}")
    return "; ".join(pairs) if pairs else ""


def _fetch_rate_limited(
    provider_key: str,
    label: str,
    base_url: str,
    api_key: str | None,
    *,
    use_proxy: bool,
) -> ProviderBalance:
    """Generic rate-limit probe for OpenAI-compatible providers
    (Groq, Cerebras, MiMo). Calls ``/models`` — a cheap GET that
    counts against the quota but returns headers with remaining
    budgets. Most of these providers don't publish a prepaid
    balance API, so rate-limit remaining is the best proxy for
    'how much can I still do'."""
    if not api_key:
        return ProviderBalance(
            key=provider_key, label=label,
            status="not_configured", headline="ключ не настроен",
        )
    try:
        with httpx.Client(**_http_client_kwargs(use_proxy=use_proxy)) as client:
            resp = client.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("provider_balances: %s fetch failed: %s", provider_key, exc)
        return ProviderBalance(
            key=provider_key, label=label,
            status="error", headline="ошибка запроса",
            details=f"{type(exc).__name__}: {str(exc)[:80]}",
        )
    summary = _read_rate_limit_headers(resp.headers)
    if not summary:
        return ProviderBalance(
            key=provider_key, label=label,
            status="not_supported",
            headline="нет rate-limit headers",
        )
    return ProviderBalance(
        key=provider_key, label=label,
        status="ok",
        headline="rate-limit remaining",
        details=summary,
    )


def _fetch_mimo(settings: Settings) -> ProviderBalance:
    """MiMo doesn't publish a standard balance endpoint (we queried
    token-plan-sgp.xiaomimimo.com earlier and found nothing). Mark
    explicitly as 'not_supported' so the UI doesn't show a scary
    error — this is expected state, not a regression."""
    key = settings.resolve_mimo_api_key()
    if not key:
        return ProviderBalance(
            key="mimo", label="MiMo-V2-Pro",
            status="not_configured", headline="ключ не настроен",
        )
    # Some deployments CAN expose rate-limit headers — try once before
    # giving up. Goes through the same outbound proxy as chat calls.
    probe = _fetch_rate_limited(
        "mimo", "MiMo-V2-Pro",
        settings.mimo_base_url, key, use_proxy=True,
    )
    if probe.status == "ok":
        return probe
    return ProviderBalance(
        key="mimo", label="MiMo-V2-Pro",
        status="not_supported",
        headline="нет публичного billing API",
        details="ключ настроен, лимиты смотреть в консоли провайдера",
    )


def _fetch_groq(settings: Settings) -> ProviderBalance:
    return _fetch_rate_limited(
        "groq", "Groq",
        "https://api.groq.com/openai/v1",
        settings.resolve_groq_api_key(),
        use_proxy=True,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_balances(settings: Settings, *, force_refresh: bool = False) -> list[ProviderBalance]:
    """Return one ``ProviderBalance`` per supported provider, cached
    60 seconds so rapid admin refreshes don't eat the free-tier
    request budget we're trying to measure."""
    cache_key = "balances"
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(cache_key)
        if not force_refresh and hit is not None and (now - hit[1]) < _CACHE_TTL:
            return hit[0]

    balances = [
        _fetch_openrouter(settings),
        _fetch_mimo(settings),
        _fetch_groq(settings),
    ]

    with _cache_lock:
        _cache[cache_key] = (balances, now)
    return balances


def invalidate_cache() -> None:
    with _cache_lock:
        _cache.clear()
