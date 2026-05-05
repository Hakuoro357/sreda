"""MaxClient — httpx async wrapper для MAX Bot API.

API contract: see ``docs/research/max_api_contracts.md`` (Phase 0 findings).

Mirrors TelegramClient pattern (`sreda.integrations.telegram.client`):
- per-token module-level connection pool
- async send_message / get_me / set_webhook / get_updates
- explicit timeouts per call
- MaxDeliveryError для нерегламентированных responses

Differences from Telegram:
- Auth header: `Authorization: <token>` (БЕЗ Bearer)
- Endpoint: ``platform-api.max.ru`` (RU; в prod должен быть в NO_PROXY)
- Send: POST ``/messages`` с recipient объектом + text + format + attachments
- Webhook: POST ``/subscriptions`` (registration); GET — list, DELETE — remove
- Long-poll ``GET /updates`` — DEV ONLY (per docs «Webhook only for production»)
- Update cursor: ``marker`` (а не update_id из TG)
- 200-status response может (или не может — TBD probe) включать error envelope

Phase 0 lock-ins documented in ``docs/research/max_api_contracts.md``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


BASE_URL = "https://platform-api.max.ru"


# Per-token httpx pool. См. integrations/telegram/client.py для rationale —
# те же соображения по keep-alive, fresh-handshake, и cross-loop safety.
_CLIENT_POOL: dict[str, httpx.AsyncClient] = {}


def _make_pool_client() -> httpx.AsyncClient:
    """httpx.AsyncClient с conservative настройками.

    keepalive=0 как в TG-клиенте (lessons из incident'a 2026-04-30 — stale
    connections через SOCKS5). MAX endpoint должен быть в ``NO_PROXY``,
    поэтому SOCKS5 не задействован, но защищаемся от любых зомби.
    """
    return httpx.AsyncClient(
        trust_env=True,
        timeout=httpx.Timeout(30.0, connect=10.0),
        limits=httpx.Limits(
            max_keepalive_connections=0,
            max_connections=20,
            keepalive_expiry=5.0,
        ),
    )


def _get_pool_client(token: str) -> httpx.AsyncClient:
    client = _CLIENT_POOL.get(token)
    if client is None or client.is_closed:
        client = _make_pool_client()
        _CLIENT_POOL[token] = client
    return client


async def close_pool() -> None:
    """Close all pooled clients. Best to call on graceful shutdown."""
    for client in list(_CLIENT_POOL.values()):
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            logger.debug("max close_pool: aclose failed (ignored)", exc_info=True)
    _CLIENT_POOL.clear()


class MaxDeliveryError(Exception):
    """Raised when MAX API call fails after retries OR returns non-success.

    ``status_code`` set when failure was HTTP status; None для timeouts/network.
    ``method`` — API path для caller-side dispatch.
    """

    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.status_code = status_code


class MaxClient:
    """Thin async wrapper над MAX Bot API.

    Каждый instance держит token; httpx-клиент per-token shared via
    module-level pool. Caller отвечает за вызов ``await close_pool()``
    на graceful shutdown (lifespan hook в main.py делает это).
    """

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("MaxClient token is required")
        self._token = token

    @property
    def token(self) -> str:
        return self._token

    # ─── Internal HTTP helpers ────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        # NB: НЕ Bearer — просто token в Authorization (per docs).
        return {
            "Authorization": self._token,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> Any:
        client = _get_pool_client(self._token)
        url = f"{BASE_URL}{path}"
        try:
            resp = await client.request(
                method, url,
                headers=self._headers(),
                params=params, json=json_payload,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise MaxDeliveryError(
                f"timeout: {exc}", method=path, status_code=None,
            ) from exc
        except httpx.RequestError as exc:
            raise MaxDeliveryError(
                f"network: {exc}", method=path, status_code=None,
            ) from exc

        if resp.status_code >= 400:
            body = resp.text[:500]
            raise MaxDeliveryError(
                f"{resp.status_code}: {body}",
                method=path, status_code=resp.status_code,
            )
        # MAX response — direct JSON, без `{ok: ...}` envelope (см. probe).
        # Если в будущем обнаружим envelope или "ok=false" pattern — добавим
        # check здесь (lessons PM.2 из TG client'a).
        try:
            return resp.json()
        except Exception:
            return resp.text

    # ─── Public API ───────────────────────────────────────────────────

    async def get_me(self) -> dict:
        """GET /me — bot identity / health-check.

        Returns ``{user_id, first_name, username, is_bot, ...}``.
        """
        return await self._request("GET", "/me", timeout=5.0)

    async def send_message(
        self,
        recipient: dict,
        text: str,
        *,
        format: str | None = None,
        attachments: list[dict] | None = None,
    ) -> dict:
        """POST /messages — send a text/attachment message.

        Recipient routing per probe 2026-05-05 (Boris прямой curl на проде):
        MAX ожидает ``chat_id`` или ``user_id`` в **query string**, НЕ
        в JSON body. ``{"recipient": {"chat_id": ...}}`` в body отвергается
        с ``"Unknown recipient"``. Корректный shape:
        ``POST /messages?chat_id=...`` body: ``{"text": "..."}``.

        ``recipient`` dict теперь интерпретируется как «один из ключей
        chat_id/user_id» — извлекаем + кладём в params. Это сохраняет
        backward-compat сигнатуру для caller'ов.

        ``format`` — optional ``"markdown"`` / ``"html"``.
        ``attachments`` — optional list, including inline-buttons keyboard.

        Raises ValueError если ни chat_id ни user_id в recipient.
        """
        params: dict[str, Any] = {}
        if "chat_id" in recipient and recipient["chat_id"] is not None:
            params["chat_id"] = recipient["chat_id"]
        elif "user_id" in recipient and recipient["user_id"] is not None:
            params["user_id"] = recipient["user_id"]
        else:
            raise ValueError(
                "MaxClient.send_message: recipient must contain "
                "non-None chat_id or user_id"
            )

        payload: dict[str, Any] = {"text": text}
        if format:
            payload["format"] = format
        if attachments:
            payload["attachments"] = attachments
        return await self._request(
            "POST", "/messages",
            params=params, json_payload=payload, timeout=10.0,
        )

    async def download_audio(self, url: str, *, timeout: float = 15.0) -> bytes:
        """Download voice/audio attachment from a signed MAX URL.

        Probe 2026-05-05: incoming voice message contains
        ``message.body.attachments[].payload.url`` — signed URL of form
        ``https://a.oneme.ru/audio?cid=...&signatureToken=...&expires=<ms>``.
        Сразу готовый к download — НИЧЕГО не нужно (никакого
        ``getFile``-аналога как в TG). Signature + expires в query
        params (≈24h окна).

        Auth header **не нужен** — наоборот, наличие ``Authorization:
        <token>`` может быть подозрительным для CDN. Используем чистый
        httpx-клиент без наших headers.

        Returns raw audio bytes. Raises ``MaxDeliveryError`` на network
        ошибки и не-200 status.
        """
        import httpx as _httpx

        try:
            async with _httpx.AsyncClient(
                timeout=_httpx.Timeout(timeout, connect=5.0),
                trust_env=True,
            ) as client:
                resp = await client.get(url)
        except _httpx.TimeoutException as exc:
            raise MaxDeliveryError(
                f"audio download timeout: {exc}",
                method="audio_download", status_code=None,
            ) from exc
        except _httpx.RequestError as exc:
            raise MaxDeliveryError(
                f"audio download network: {exc}",
                method="audio_download", status_code=None,
            ) from exc

        if resp.status_code != 200:
            body = resp.text[:300] if resp.text else "<empty>"
            raise MaxDeliveryError(
                f"audio download {resp.status_code}: {body}",
                method="audio_download", status_code=resp.status_code,
            )
        return resp.content

    async def get_updates(
        self,
        *,
        marker: int | None = None,
        limit: int = 100,
        timeout: int = 30,
    ) -> dict:
        """GET /updates — DEV ONLY long-poll.

        Production использует webhook (set_webhook). Этот метод — только для
        probe + локальный dev. Не вызывать в prod (см. docs «only Webhook
        for production»).

        ``marker`` — server-side cursor (аналог TG ``offset``).
        ``timeout=0`` → non-blocking; >0 → блокирует до timeout секунд.
        """
        params: dict[str, Any] = {"limit": limit, "timeout": timeout}
        if marker is not None:
            params["marker"] = marker
        # Client-side timeout = server timeout + 5s buffer для slow-network.
        return await self._request(
            "GET", "/updates", params=params, timeout=float(timeout + 5),
        )

    async def set_webhook(
        self,
        url: str,
        *,
        update_types: list[str] | None = None,
        secret_token: str | None = None,
    ) -> dict:
        """POST /subscriptions — register webhook URL.

        Payload field per MAX docs (2026-05-04 confirmed):
        - ``url`` — webhook endpoint
        - ``update_types`` — optional list filter
        - ``secret`` — optional shared secret. MAX будет echo'ить его в
          ``X-Max-Bot-Api-Secret`` header каждого webhook POST'а.
          Recommended (non-empty → header-based verification возможен).

        Note: keyword-arg в Python остаётся ``secret_token`` для совместимости
        с TG-style nomenclature нашего вызывающего кода; на wire он мапится
        в MAX-specific ``secret``.
        """
        payload: dict[str, Any] = {"url": url}
        if update_types:
            payload["update_types"] = update_types
        if secret_token:
            payload["secret"] = secret_token
        return await self._request(
            "POST", "/subscriptions", json_payload=payload, timeout=10.0,
        )

    async def delete_webhook(self, url: str | None = None) -> dict:
        """DELETE /subscriptions — unregister webhook."""
        params = {"url": url} if url else None
        return await self._request(
            "DELETE", "/subscriptions", params=params, timeout=5.0,
        )

    async def get_subscriptions(self) -> dict:
        """GET /subscriptions — list registered webhooks."""
        return await self._request("GET", "/subscriptions", timeout=5.0)

    async def aclose(self) -> None:
        """No-op: real close через ``close_pool()`` module-level
        (TG-pattern). Provided for symmetric instance API."""
        return None
