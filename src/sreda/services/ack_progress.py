"""Ack-message progress editing helpers.

The inbound layer sends the first ack immediately. This controller owns
subsequent best-effort edits and the final "edit ack into answer" UX.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from typing import Any, Awaitable

from sreda.services import trace
from sreda.services.ack_messages import FINAL_PROGRESS_TEXT, pick_progress_ack

logger = logging.getLogger(__name__)


class AckProgressController:
    """Serialize progress edits against one already-sent ack message."""

    def __init__(
        self,
        *,
        ack_message_id_future: Awaitable[Any],
        enabled: bool,
    ) -> None:
        self._ack_message_id_future = ack_message_id_future
        self.enabled = bool(enabled)
        self._tail: asyncio.Task | None = None
        self._previous_progress: str | None = None
        self._final_edit_planned = False
        self._last_stream_edit_at = 0.0
        self._last_stream_text: str | None = None
        self._last_stream_min_visible_seconds = 0.0

    @property
    def final_edit_planned(self) -> bool:
        return self._final_edit_planned

    def mark_final_edit_planned(self) -> None:
        self._final_edit_planned = True

    async def ack_message_id(self, *, timeout_seconds: float = 2.0) -> Any | None:
        if not inspect.isawaitable(self._ack_message_id_future):
            return self._ack_message_id_future
        try:
            return await asyncio.wait_for(
                asyncio.shield(self._ack_message_id_future),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.debug("ack progress: ack id timeout")
            return None
        except Exception:  # noqa: BLE001
            logger.debug("ack progress: ack id unavailable", exc_info=True)
            return None

    def schedule_progress(self, text: str | None = None) -> None:
        if not self.enabled:
            return
        phrase = text or pick_progress_ack(previous=self._previous_progress)
        self._previous_progress = phrase
        self._schedule_edit(phrase, event_name="ack.progress_edit")

    def schedule_almost_done(self) -> None:
        self.schedule_progress(FINAL_PROGRESS_TEXT)

    def schedule_stream_text(
        self,
        text: str,
        *,
        min_interval_seconds: float = 0.8,
        force: bool = False,
    ) -> None:
        if not self.enabled:
            return
        normalized = text.strip()
        if not normalized or normalized == self._last_stream_text:
            return
        now = time.monotonic()
        if (
            not force
            and self._last_stream_edit_at
            and now - self._last_stream_edit_at < min_interval_seconds
        ):
            return
        self._last_stream_edit_at = now
        self._last_stream_text = normalized
        self._last_stream_min_visible_seconds = min_interval_seconds
        self._schedule_edit(normalized, event_name="ack.stream_edit")

    def has_stream_text(self) -> bool:
        return bool(self._last_stream_text)

    def is_stream_text_current(self, text: str) -> bool:
        return bool(self._last_stream_text) and self._last_stream_text == text.strip()

    async def flush_stream_final_text(self, text: str) -> None:
        if not self.enabled or not self.has_stream_text():
            return
        self.schedule_stream_text(text, min_interval_seconds=0, force=True)
        await self.drain()

    async def keep_stream_partial_visible(self, final_text: str) -> None:
        if not self._last_stream_text or self._last_stream_text == final_text.strip():
            return
        min_visible = min(max(self._last_stream_min_visible_seconds, 0.0), 0.5)
        if min_visible <= 0:
            return
        elapsed = time.monotonic() - self._last_stream_edit_at
        remaining = min_visible - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)

    def _schedule_edit(self, text: str, *, event_name: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("ack progress: no running loop; skip edit")
            return
        prev = self._tail
        self._tail = loop.create_task(
            self._run_after(prev, text, event_name=event_name),
            name="ack_progress_edit",
        )

    async def _run_after(
        self,
        prev: asyncio.Task | None,
        text: str,
        *,
        event_name: str,
    ) -> None:
        if prev is not None:
            with contextlib.suppress(Exception):
                await prev
        message_id = await self.ack_message_id()
        if not message_id:
            trace.record(event_name, status="no_ack")
            return
        try:
            await self._edit_once(message_id, text=text, reply_markup=None)
            trace.record(event_name, status="ok")
        except Exception as exc:  # noqa: BLE001
            logger.debug("ack progress edit failed: %s", exc)
            trace.record(event_name, status="failed")

    async def drain(self) -> None:
        if self._tail is None:
            return
        with contextlib.suppress(Exception):
            await self._tail

    async def _edit_once(
        self,
        message_id: Any,
        *,
        text: str,
        reply_markup: dict | None,
    ) -> Any:
        raise NotImplementedError


class TelegramAckProgressController(AckProgressController):
    def __init__(
        self,
        *,
        telegram_client: Any,
        chat_id: str,
        ack_message_id_future: Awaitable[Any],
        enabled: bool,
    ) -> None:
        super().__init__(
            ack_message_id_future=ack_message_id_future,
            enabled=enabled,
        )
        self.telegram = telegram_client
        self.chat_id = str(chat_id)

    async def _edit_once(
        self,
        message_id: Any,
        *,
        text: str,
        reply_markup: dict | None,
    ) -> Any:
        return await self.telegram.edit_message_text(
            chat_id=self.chat_id,
            message_id=int(message_id),
            text=text,
            reply_markup=reply_markup,
        )

    async def edit_final_or_fallback(
        self,
        text: str,
        *,
        reply_markup: dict | None = None,
    ) -> str:
        if self.enabled:
            await self.drain()
            await self.keep_stream_partial_visible(text)
            message_id = await self.ack_message_id()
            if message_id:
                self.mark_final_edit_planned()
                if reply_markup is None and self.is_stream_text_current(text):
                    trace.record("ack.final_edit", status="skipped_same_text")
                    return "edited"
                for attempt in range(2):
                    try:
                        await self._edit_once(
                            message_id, text=text, reply_markup=reply_markup,
                        )
                        trace.record("ack.final_edit", status="ok")
                        return "edited"
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "telegram ack final edit failed attempt=%d: %s",
                            attempt + 1, exc,
                        )
                        if attempt == 0:
                            await asyncio.sleep(0.2)
                trace.record("ack.final_edit", status="failed")
            else:
                trace.record("ack.final_edit", status="no_ack")

        await self.telegram.send_message(
            chat_id=self.chat_id,
            text=text,
            reply_markup=reply_markup,
        )
        if self.enabled and not message_id:
            message_id = await self.ack_message_id(timeout_seconds=10.0)
        if self.enabled and message_id:
            try:
                await self.telegram.delete_message(
                    chat_id=self.chat_id,
                    message_id=int(message_id),
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "telegram ack fallback delete failed message_id=%s",
                    message_id,
                    exc_info=True,
                )
        trace.record("ack.final_fallback", status="sent")
        return "fallback_sent"


class MaxAckProgressController(AckProgressController):
    def __init__(
        self,
        *,
        max_client: Any,
        ack_message_id_future: Awaitable[Any],
        enabled: bool,
    ) -> None:
        super().__init__(
            ack_message_id_future=ack_message_id_future,
            enabled=enabled,
        )
        self.max = max_client

    async def _edit_once(
        self,
        message_id: Any,
        *,
        text: str,
        reply_markup: dict | None,
    ) -> Any:
        _ = reply_markup
        return await self.max.edit_message(str(message_id), text=text)
