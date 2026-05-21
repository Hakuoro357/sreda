from __future__ import annotations

import asyncio
import time

from sreda.services.ack_progress import TelegramAckProgressController


class _FakeTelegram:
    def __init__(self) -> None:
        self.edits: list[dict] = []
        self.sent: list[dict] = []
        self.deleted: list[dict] = []
        self.fail_edit_once = False
        self.fail_edit_always = False

    async def edit_message_text(self, *, chat_id, message_id, text, reply_markup=None):
        self.edits.append({
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "reply_markup": reply_markup,
        })
        if self.fail_edit_once:
            self.fail_edit_once = False
            raise RuntimeError("rate limited")
        if self.fail_edit_always:
            raise RuntimeError("message disappeared")
        return {"ok": True}

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.sent.append({
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
        })
        return {"ok": True, "result": {"message_id": 777}}

    async def delete_message(self, *, chat_id, message_id):
        self.deleted.append({"chat_id": chat_id, "message_id": message_id})
        return {"ok": True}


def test_telegram_final_edit_retries_once_before_success():
    tg = _FakeTelegram()
    tg.fail_edit_once = True
    controller = TelegramAckProgressController(
        telegram_client=tg,
        chat_id="42",
        ack_message_id_future=123,
        enabled=True,
    )

    result = asyncio.run(controller.edit_final_or_fallback("Готово"))

    assert result == "edited"
    assert [item["text"] for item in tg.edits] == ["Готово", "Готово"]
    assert tg.sent == []


def test_telegram_final_edit_falls_back_to_send_when_ack_disappeared():
    tg = _FakeTelegram()
    tg.fail_edit_always = True
    controller = TelegramAckProgressController(
        telegram_client=tg,
        chat_id="42",
        ack_message_id_future=123,
        enabled=True,
    )

    result = asyncio.run(controller.edit_final_or_fallback("Готово"))

    assert result == "fallback_sent"
    assert len(tg.edits) == 2
    assert tg.sent == [{"chat_id": "42", "text": "Готово", "reply_markup": None}]
    assert tg.deleted == [{"chat_id": "42", "message_id": 123}]


def test_telegram_final_fallback_deletes_delayed_ack_message():
    class _DelayedAckController(TelegramAckProgressController):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.calls = 0

        async def ack_message_id(self, *, timeout_seconds: float = 2.0):
            self.calls += 1
            return None if self.calls == 1 else 123

    tg = _FakeTelegram()
    controller = _DelayedAckController(
        telegram_client=tg,
        chat_id="42",
        ack_message_id_future=123,
        enabled=True,
    )

    result = asyncio.run(controller.edit_final_or_fallback("Готово"))

    assert result == "fallback_sent"
    assert tg.edits == []
    assert tg.sent == [{"chat_id": "42", "text": "Готово", "reply_markup": None}]
    assert tg.deleted == [{"chat_id": "42", "message_id": 123}]


def test_telegram_progress_disabled_is_noop():
    tg = _FakeTelegram()
    controller = TelegramAckProgressController(
        telegram_client=tg,
        chat_id="42",
        ack_message_id_future=123,
        enabled=False,
    )
    controller.schedule_progress("Смотрю")

    asyncio.run(controller.drain())

    assert tg.edits == []
    assert tg.sent == []


def test_telegram_stream_text_updates_ack_message():
    tg = _FakeTelegram()
    controller = TelegramAckProgressController(
        telegram_client=tg,
        chat_id="42",
        ack_message_id_future=123,
        enabled=True,
    )

    async def _run():
        controller.schedule_stream_text("При", min_interval_seconds=0)
        await controller.drain()
        controller.schedule_stream_text("Привет", min_interval_seconds=0)
        await controller.drain()

    asyncio.run(_run())

    assert [item["text"] for item in tg.edits] == ["При", "Привет"]


def test_telegram_final_edit_keeps_stream_partial_visible_briefly():
    tg = _FakeTelegram()
    controller = TelegramAckProgressController(
        telegram_client=tg,
        chat_id="42",
        ack_message_id_future=123,
        enabled=True,
    )

    async def _run():
        controller.schedule_stream_text("При", min_interval_seconds=0.05)
        await controller.drain()
        start = time.monotonic()
        result = await controller.edit_final_or_fallback("Привет")
        return result, time.monotonic() - start

    result, elapsed = asyncio.run(_run())

    assert result == "edited"
    assert [item["text"] for item in tg.edits] == ["При", "Привет"]
    assert elapsed >= 0.04


def test_telegram_final_edit_skips_when_stream_already_has_same_text():
    tg = _FakeTelegram()
    controller = TelegramAckProgressController(
        telegram_client=tg,
        chat_id="42",
        ack_message_id_future=123,
        enabled=True,
    )

    async def _run():
        controller.schedule_stream_text("Привет", min_interval_seconds=0)
        await controller.drain()
        return await controller.edit_final_or_fallback("Привет")

    result = asyncio.run(_run())

    assert result == "edited"
    assert [item["text"] for item in tg.edits] == ["Привет"]
    assert tg.sent == []
