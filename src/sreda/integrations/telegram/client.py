from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)


class TelegramDeliveryError(Exception):
    pass


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
        try:
            async with httpx.AsyncClient(timeout=15.0, trust_env=True) as client:
                response = await client.get(url)
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
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                async with httpx.AsyncClient(timeout=timeout, trust_env=True) as client:
                    response = await client.post(url, json=json, data=data, files=files)
                    response.raise_for_status()
                    return response.json()
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                last_error = exc
                logger.warning(
                    "Telegram request failed: method=%s attempt=%s error=%s",
                    method,
                    attempt,
                    exc,
                )
                if attempt < 3:
                    await asyncio.sleep(0.5 * attempt)
        raise TelegramDeliveryError(f"Telegram request failed for {method}") from last_error
