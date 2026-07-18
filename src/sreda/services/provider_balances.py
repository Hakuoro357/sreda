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

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from sreda.config.settings import Settings

logger = logging.getLogger(__name__)


_CACHE_TTL = 60.0
_cache_lock = threading.Lock()
# Single-flight для самих фетчей (2026-07-18 audit #7): _cache_lock держится
# только на чтение/запись dict, поэтому параллельные refresh'ы админки шли в
# сеть пачкой — каждый /models-проба сам тратил тот rate-limit, который
# измеряет. Отдельный lock: фетчи до ~50 с (5 провайдеров × timeout 10 с),
# держать _cache_lock на это время нельзя (invalidate_cache бы висел).
_fetch_lock = threading.Lock()
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


def _fetch_inception(settings: Settings) -> ProviderBalance:
    """Inception (Mercury-2, «Фредди») — провайдер планировщика Среды.
    Публичного billing/balance API у Inception нет; провайдера планировщика
    НАМЕРЕННО не дёргаем сетью на каждый показ админки (в отличие от
    openrouter/groq/mimo). Оценку трат в USD смотреть на /admin/budget и
    /admin/spend-by-model — там Inception priced по нашему прайсу (#150)."""
    key = settings.resolve_inception_api_key()
    if not key:
        return ProviderBalance(
            key="inception-mercury2", label="Inception · Mercury-2 (Фредди, планировщик)",
            status="not_configured", headline="ключ не настроен",
        )
    return ProviderBalance(
        key="inception-mercury2", label="Inception · Mercury-2 (Фредди, планировщик)",
        status="not_supported",
        headline="нет публичного billing API",
        details="оценка трат в USD — на /admin/budget и /admin/spend-by-model",
    )


# ---------------------------------------------------------------------------
# Yandex Cloud billing balance (#229) — SA key → JWT(PS256) → IAM → Billing
# ---------------------------------------------------------------------------

_YANDEX_IAM_ENDPOINT = "https://iam.api.cloud.yandex.net/iam/v1/tokens"
_YANDEX_BILLING_ENDPOINT = "https://billing.api.cloud.yandex.net/billing/v1/billingAccounts"
# IAM tokens live 12h; we refresh after ~50min (3000s) — well before expiry,
# and before the 1h JWT `exp` we sign — so the cached token is always valid.
_IAM_TTL = 3000.0
_iam_lock = threading.Lock()
_iam_cache: dict[str, tuple[str, float]] = {}


def _parse_yandex_sa_key(raw: str, sa_id_fallback: str | None) -> tuple[str, str, str]:
    """Extract ``(key_id, service_account_id, private_key_pem)`` from an
    authorized-key file. Two console download formats are accepted:

    * **JSON** («скачать ключ как JSON») — has ``id`` /
      ``service_account_id`` / ``private_key``.
    * **PEM with a Yandex header** «… SA Key ID <id>» («скачать приватный
      ключ») — key id is in the header (the console sometimes wraps it in
      ``<…>`` — strip it); the service-account id is NOT in the file, so it
      must come from ``sa_id_fallback`` (``SREDA_YANDEX_BILLING_SA_ID``).
    """
    raw = raw.strip()
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, dict) and obj.get("private_key"):
        key_id = (obj.get("id") or "").strip()
        sa_id = (obj.get("service_account_id") or sa_id_fallback or "").strip()
        pem = obj["private_key"]
    else:
        m = re.search(r"Key ID\s*<?([A-Za-z0-9]+)>?", raw)
        key_id = m.group(1) if m else ""
        sa_id = (sa_id_fallback or "").strip()
        i = raw.find("-----BEGIN")
        pem = raw[i:].strip() if i >= 0 else ""
    if not (key_id and sa_id and pem):
        raise ValueError(
            "incomplete Yandex SA key (key_id=%s sa_id=%s pem=%s; PEM-format "
            "keys need SREDA_YANDEX_BILLING_SA_ID)"
            % ("ok" if key_id else "missing",
               "ok" if sa_id else "missing",
               "ok" if pem else "missing")
        )
    return key_id, sa_id, pem


def _build_yandex_jwt(key_id: str, sa_id: str, private_pem: str) -> str:
    """Sign a PS256 JWT used to obtain an IAM token from the SA key."""
    import base64

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    def b64u(data: bytes) -> bytes:
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    now = int(time.time())
    header = {"alg": "PS256", "typ": "JWT", "kid": key_id}
    payload = {"iss": sa_id, "aud": _YANDEX_IAM_ENDPOINT, "iat": now, "exp": now + 3600}
    signing_input = (
        b64u(json.dumps(header, separators=(",", ":")).encode())
        + b"."
        + b64u(json.dumps(payload, separators=(",", ":")).encode())
    )
    private_key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    signature = private_key.sign(
        signing_input,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return (signing_input + b"." + b64u(signature)).decode()


def _mint_yandex_iam(settings: Settings) -> str:
    with open(settings.yandex_billing_sa_key_file, encoding="utf-8-sig") as fh:
        raw = fh.read()
    key_id, sa_id, pem = _parse_yandex_sa_key(raw, settings.yandex_billing_sa_id)
    jwt_token = _build_yandex_jwt(key_id, sa_id, pem)
    with httpx.Client(**_http_client_kwargs(use_proxy=False)) as client:
        resp = client.post(_YANDEX_IAM_ENDPOINT, json={"jwt": jwt_token})
        resp.raise_for_status()
        token = resp.json().get("iamToken")
    if not token:
        raise ValueError("IAM token missing in exchange response")
    return token


def _yandex_iam_token(settings: Settings) -> str:
    """Return a cached IAM token (minted lazily; refreshed after ~50min)."""
    cache_key = settings.yandex_billing_sa_key_file or ""
    now = time.monotonic()
    with _iam_lock:
        hit = _iam_cache.get(cache_key)
        if hit is not None and now < hit[1]:
            return hit[0]
    token = _mint_yandex_iam(settings)
    with _iam_lock:
        _iam_cache[cache_key] = (token, now + _IAM_TTL)
    return token


def invalidate_iam_cache() -> None:
    with _iam_lock:
        _iam_cache.clear()


def _fetch_yandex(settings: Settings) -> ProviderBalance:
    """Yandex Cloud прямой остаток ₽ (покрывает SpeechKit STT, #229).
    Авторизация: авторизованный ключ СА → PS256 JWT → IAM-токен → Billing
    API. В отличие от Groq/OpenRouter ходим НАПРЯМУЮ (Яндекс — RU-сервис, не
    геоблочит), как и speech-вызов.

    Защита (R1-ревью): весь разбор внутри ``try`` (битый ответ не валит
    остальные провайдеры); error-details ОБЕЗЛИЧЕНЫ и в лог идёт только класс
    исключения — это секрет-смежный путь (ключ/IAM-токен), сырой ``str(exc)``
    мог бы протечь содержимое ключа при кривом конфиге (путь = сам ключ)."""
    label = "Яндекс · SpeechKit (STT)"
    if not settings.yandex_billing_sa_key_file:
        return ProviderBalance(
            key="yandex", label=label,
            status="not_configured", headline="ключ не настроен",
        )

    def _err(headline: str, details: str = "") -> ProviderBalance:
        return ProviderBalance(
            key="yandex", label=label, status="error",
            headline=headline, details=details,
        )

    try:
        token = _yandex_iam_token(settings)
        with httpx.Client(**_http_client_kwargs(use_proxy=False)) as client:
            resp = client.get(
                _YANDEX_BILLING_ENDPOINT,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            payload = resp.json()

        raw_accounts = payload.get("billingAccounts") if isinstance(payload, dict) else None
        accounts = [a for a in (raw_accounts or []) if isinstance(a, dict)]
        if not accounts:
            return _err(
                "нет доступа к платёжному аккаунту",
                "проверь роль billing.accounts.viewer у сервисного аккаунта",
            )

        want = settings.yandex_billing_account_id
        if want:
            account = next((a for a in accounts if a.get("id") == want), None)
            if account is None:
                return _err(
                    "платёжный аккаунт не найден",
                    f"SREDA_YANDEX_BILLING_ACCOUNT_ID={want} отсутствует в списке",
                )
        else:
            account = accounts[0]

        raw_balance = account.get("balance")
        if raw_balance is None:
            return _err("баланс не получен", "ответ Billing API без поля balance")
        balance = Decimal(str(raw_balance))  # InvalidOperation → ловится ниже
    except Exception as exc:  # noqa: BLE001
        # Только класс исключения — НЕ str(exc) (секрет-смежный путь).
        logger.warning("provider_balances: Yandex fetch failed: %s", type(exc).__name__)
        return _err("ошибка запроса", "не удалось получить баланс Yandex Billing")

    currency = account.get("currency") or "RUB"
    name = account.get("name") or account.get("id") or ""
    details = f"аккаунт {name}" + ("" if account.get("active", True) else " (НЕактивен!)")
    return ProviderBalance(
        key="yandex", label=label,
        status="ok", headline=f"{balance:.2f} {currency}", details=details,
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

    # Single-flight: дедуплицируем параллельные refresh'ы админки. Проигравшие
    # ждут победителя и забирают его свежий кэш (double-checked ниже).
    with _fetch_lock:
        with _cache_lock:
            hit = _cache.get(cache_key)
            if not force_refresh and hit is not None and (now - hit[1]) < _CACHE_TTL:
                return hit[0]

        balances = [
            _fetch_openrouter(settings),
            _fetch_inception(settings),   # планировщик «Фредди» (#150 follow-up)
            _fetch_mimo(settings),
            _fetch_groq(settings),
            _fetch_yandex(settings),      # STT + биллинг, реальный остаток ₽ (#229)
        ]

        # 2026-07-18 audit fix (#7): метка кэша — ПОСЛЕ сетевых фетчей, а не
        # до. Раньше запись ложилась почти протухшей (фетчи до ~50 с при TTL
        # 60 с) и следующий refresh снова шёл в сеть.
        fetched_at = time.monotonic()
        with _cache_lock:
            _cache[cache_key] = (balances, fetched_at)
        return balances


def invalidate_cache() -> None:
    with _cache_lock:
        _cache.clear()
