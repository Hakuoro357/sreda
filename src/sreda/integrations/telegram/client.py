from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# 2026-04-29: per-process httpx.AsyncClient pool keyed by bot token.
# Воссоздание httpx.AsyncClient на каждый запрос требует нового TCP +
# TLS handshake (~150-300мс через SOCKS5 egress). voice flow делает 4
# Telegram-вызова подряд (sendMessage ack, getFile, download, sendMessage
# reply) — это давало ~600-1200мс лишней латентности.
#
# Pool кэширует одного клиента на токен; httpx сам поддерживает
# keep-alive в connection pool. На shutdown процесса OS закрывает FDs
# (не lift'аем lifespan-hook ради этого, но при необходимости можно
# вызвать close_pool()).
#
# Безопасность по event loop: pool — module-level dict, его держит ОДИН
# процесс. uvicorn держит один loop; job_runner — отдельный процесс
# (свой loop, свой dict). Cross-loop проблем нет.
_CLIENT_POOL: dict[str, httpx.AsyncClient] = {}


def _make_pool_client() -> httpx.AsyncClient:
    """Build httpx.AsyncClient suitable for keep-alive Telegram traffic."""
    return httpx.AsyncClient(
        trust_env=True,
        # Defaults; per-call timeout passed via .request(timeout=)
        timeout=httpx.Timeout(30.0, connect=10.0),
        limits=httpx.Limits(
            # 2026-04-30 (incident: ack/sendMessage висели 30-60s на
            # stale connections): через SOCKS5 с DNS-flapping на egress
            # (89.110.77.78) keepalive connections превращались в
            # zombies. Pool отдавал «живой» connection, httpx писал
            # request, ssh-channel пытался открыться → DNS-fail → 30s
            # timeout. Пинг-таска лишь усугубляла, refreshя зомби.
            #
            # Откат: max_keepalive=0, никакого keepalive. Каждый
            # request — fresh TCP+TLS handshake (~800ms через SOCKS).
            # Медленнее но детерминистично, никаких 30-60s залипов.
            # Когда SOCKS станет стабильным или станем direct-only —
            # можно вернуть keepalive=10/expiry=60.
            max_keepalive_connections=0,
            max_connections=20,
            keepalive_expiry=5.0,
        ),
    )


def _get_pool_client(token: str) -> httpx.AsyncClient:
    """Lazy-init cached client per token."""
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
            logger.debug("close_pool: aclose failed (ignored)", exc_info=True)
    _CLIENT_POOL.clear()


class TelegramDeliveryError(Exception):
    """Raised when Telegram API call fails after retries (or on non-retryable
    4xx responses). ``status_code`` is set when the failure was an HTTP
    status (400/403/429/etc.) — None for timeouts/network errors. ``method``
    holds the API method name (sendMessage / answerCallbackQuery / ...)
    for caller-side dispatch.
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


class TelegramClient:
    def __init__(self, token: str) -> None:
        self.token = token

    async def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self._post_json("sendMessage", payload, timeout=5.0)

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return await self._post_json("answerCallbackQuery", payload, timeout=3.0)

    async def edit_message_text(
        self,
        *,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> dict:
        """Rewrite an existing message in-place. Used to update a
        reminder message ("🔔 купить молоко") into its acknowledged
        form ("✅ купить молоко") and drop the inline keyboard so the
        buttons can't be tapped twice.
        """
        payload: dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._post_json("editMessageText", payload, timeout=5.0)

    async def send_chat_action(self, chat_id: str, action: str = "typing") -> dict:
        """Telegram chat-action API — показывает юзеру индикатор «печатает…».

        Действие живёт ~5 секунд на стороне Telegram, потом гаснет само.
        Если за это время бот пришлёт реальное сообщение — индикатор
        снимается мгновенно (Telegram гарантирует это поведение).

        Используется на webhook entry чтобы юзер мгновенно видел что
        бот «реагирует», ещё до того как мы успели сформулировать
        ack-message. Параллельно с этим вызовом запускается
        ack-task — обычно реальный ack приходит за 80-200мс, что
        снимает typing-индикатор естественным путём.

        Возможные ``action``: typing / record_voice / upload_document /
        find_location etc. Для нас — только ``typing``.
        """
        return await self._post_json(
            "sendChatAction",
            {"chat_id": chat_id, "action": action},
            timeout=3.0,
        )

    async def delete_message(self, chat_id: str, message_id: int) -> dict:
        """Telegram `deleteMessage` — удаляет ранее отправленное ботом
        сообщение из чата. Используется для clean-chat паттерна:
        ack-message удаляется после доставки реального reply'я,
        в чате остаётся одно сообщение на turn вместо двух.

        Bot может удалять только свои сообщения — это ограничение
        Telegram API. Фейлится 400 если message_id не существует
        или удалён юзером (best-effort delete; caller игнорирует)."""
        return await self._post_json(
            "deleteMessage",
            {"chat_id": chat_id, "message_id": message_id},
            timeout=3.0,
        )

    async def get_me(self) -> dict:
        """Telegram `getMe` — лёгкий запрос для прогрева TLS connection.

        Используется keepalive-pinger task'ом (см. `run_keepalive_pinger`)
        чтобы idle TCP+TLS не остывал в connection pool'е. Через SOCKS5
        egress fresh handshake = ~800мс, warm reuse = ~50мс. Пинг каждые
        45с держит connection живым (keepalive_expiry=300с в pool config).
        """
        return await self._post_json("getMe", {}, timeout=5.0)

    async def set_my_commands(self, commands: list[dict]) -> dict:
        return await self._post_json("setMyCommands", {"commands": commands}, timeout=10.0)

    async def send_media_group(
        self,
        chat_id: str,
        media: list[dict],
    ) -> dict:
        return await self._post_json(
            "sendMediaGroup",
            {"chat_id": chat_id, "media": media},
            timeout=10.0,
        )

    async def send_photo(
        self,
        chat_id: str,
        photo_bytes: bytes,
        *,
        filename: str = "photo.jpg",
    ) -> dict:
        return await self._post_multipart(
            "sendPhoto",
            data={"chat_id": chat_id},
            files={"photo": (filename, photo_bytes, "image/jpeg")},
            timeout=20.0,
        )

    async def get_file_info(self, file_id: str) -> dict:
        """Telegram Bot API getFile → {"file_path": "voice/file_123.oga", ...}"""
        resp = await self._post_json("getFile", {"file_id": file_id}, timeout=5.0)
        return resp.get("result", resp)

    async def download_file(self, file_path: str) -> bytes:
        """Download a file from Telegram CDN by file_path."""
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        client = _get_pool_client(self.token)
        try:
            response = await client.get(url, timeout=15.0)
            response.raise_for_status()
            return response.content
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
            raise TelegramDeliveryError(f"Failed to download file: {file_path}") from exc

    async def _post_json(self, method: str, payload: dict, *, timeout: float) -> dict:
        return await self._post_request(
            method,
            timeout=timeout,
            json=payload,
        )

    async def _post_multipart(
        self,
        method: str,
        *,
        data: dict,
        files: dict,
        timeout: float,
    ) -> dict:
        return await self._post_request(
            method,
            timeout=timeout,
            data=data,
            files=files,
        )

    async def _post_request(
        self,
        method: str,
        *,
        timeout: float,
        json: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
    ) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        client = _get_pool_client(self.token)
        last_error: Exception | None = None
        last_status: int | None = None
        for attempt in range(1, 4):
            try:
                response = await client.post(
                    url, json=json, data=data, files=files, timeout=timeout,
                )
                response.raise_for_status()
                # 2026-04-29 (incident user_tg_471032584): Telegram Bot API
                # умеет возвращать HTTP 200 с `{"ok": false, "description":
                # "..."}` (невалидный reply_markup, parse_mode-ошибка, и
                # т.п.). Раньше return'или body как есть → caller думал
                # что отправка прошла, юзер ничего не получал, в логах
                # только «200 OK». Теперь явно ловим `ok=false` и кидаем
                # TelegramDeliveryError с полным body чтобы при инциденте
                # было видно что реально вернул Telegram.
                body = response.json()
                if isinstance(body, dict) and body.get("ok") is False:
                    description = body.get("description")
                    logger.warning(
                        "Telegram %s ok=false: status=%s description=%r body=%s",
                        method, response.status_code, description,
                        str(body)[:500],
                    )
                    raise TelegramDeliveryError(
                        f"Telegram {method}: ok=false desc={description!r}",
                        method=method,
                        status_code=response.status_code,
                    )
                return body
            except httpx.HTTPStatusError as exc:
                last_error = exc
                last_status = exc.response.status_code
                logger.warning(
                    "Telegram request failed: method=%s attempt=%s status=%s error=%s",
                    method, attempt, last_status, exc,
                )
                # 2026-04-28: 4xx — non-retryable. 400 (callback expired,
                # bad request), 403 (bot blocked), 404 (chat not found),
                # 429 (rate limit) — повторные попытки только усугубят
                # rate-limit и не приведут к успеху. Failing fast убирает
                # spam loops при tap-flood / истёкших callback'ах.
                if 400 <= last_status < 500:
                    raise TelegramDeliveryError(
                        f"Telegram {method} non-retryable {last_status}",
                        method=method,
                        status_code=last_status,
                    ) from exc
                # 5xx — retryable, продолжаем
                if attempt < 3:
                    await asyncio.sleep(0.5 * attempt)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                logger.warning(
                    "Telegram request failed: method=%s attempt=%s error=%s",
                    method, attempt, exc,
                )
                if attempt < 3:
                    await asyncio.sleep(0.5 * attempt)
        raise TelegramDeliveryError(
            f"Telegram request failed for {method}",
            method=method,
            status_code=last_status,
        ) from last_error


# 2026-04-29: keepalive pinger — фоновая task которая раз в 45с пингует
# `getMe`, чтобы TCP+TLS connection не остывал в pool'е. Через SOCKS5
# egress (RU IP заблокирован Telegram'ом, ходим через 89.110.77.78)
# fresh handshake занимает 800-900мс — невыносимо для UX когда первый
# ack юзера в день этим занимается. Pinger держит connection «горячим»
# 24/7 ценой ~1920 пустых getMe'ев в день (копейки $-wise).
#
# Стратегия dumb: не трекаем last_activity, не оптимизируем — фиксированный
# интервал 45с. Это упрощает код (нет shared state, нет race conditions),
# а 1 запрос в 45с не тревожит rate-limit Telegram'а ни на йоту.
#
# DEBUG-level logging — пингов очень много, INFO засорил бы trace.

_PINGER_INTERVAL_SECONDS = 45.0


async def run_keepalive_pinger(token: str) -> None:
    """Бесконечная task: getMe → sleep 45s → repeat.

    Запускается на FastAPI lifespan startup, отменяется на shutdown
    (через asyncio.CancelledError). Сбои getMe (network glitches,
    egress down) не пробрасываются — просто log + продолжаем тикать.
    Когда egress поднимется обратно, следующий пинг прогреет
    connection заново.
    """
    client = TelegramClient(token)
    logger.info(
        "telegram keepalive pinger started: interval=%.0fs token=...%s",
        _PINGER_INTERVAL_SECONDS, token[-6:] if len(token) > 6 else "?",
    )
    try:
        while True:
            await asyncio.sleep(_PINGER_INTERVAL_SECONDS)
            try:
                await client.get_me()
                logger.debug("telegram keepalive pinger: getMe ok")
            except TelegramDeliveryError as exc:
                logger.debug(
                    "telegram keepalive pinger: getMe failed status=%s",
                    exc.status_code,
                )
            except Exception:  # noqa: BLE001
                # Defensive: pinger никогда не должен ломаться. Любая
                # неожиданная ошибка — log debug и идём дальше.
                logger.debug(
                    "telegram keepalive pinger: unexpected error",
                    exc_info=True,
                )
    except asyncio.CancelledError:
        logger.info("telegram keepalive pinger stopped")
        raise
