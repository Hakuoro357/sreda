# -*- coding: utf-8 -*-
"""#252 — поэтапный ак в react-пути: начальный «Минутку…» + временной драйвер
(случайные фразы прогресса → «Почти готово» терминал).

Голосовой ранний ак + переиспользование _early_ack_mid + финальная правка —
интеграционные (полный inbound), проверяются ревью + вживую; здесь — детерминированные
контракты драйвера и начального текста.
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_send_initial_ack_uses_minutku() -> None:
    # #252: начальный ак = «Минутку…» (был «Секунду…»), возвращает message_id.
    from sreda.services.telegram_inbound import _ACK_INITIAL_TEXT, _send_initial_ack

    sent: list[str] = []

    class FakeClient:
        async def send_message(self, *, chat_id, text):  # noqa: ANN001
            sent.append(text)
            return {"result": {"message_id": 77}}

    mid = await _send_initial_ack(FakeClient(), "123")
    assert mid == 77
    assert sent == ["Минутку…"]
    assert _ACK_INITIAL_TEXT == "Минутку…"


@pytest.mark.asyncio
async def test_send_initial_ack_failopen_returns_none() -> None:
    # Сбой отправки ак не валит ход → None.
    from sreda.services.telegram_inbound import _send_initial_ack

    class BoomClient:
        async def send_message(self, *, chat_id, text):  # noqa: ANN001
            raise RuntimeError("network")

    assert await _send_initial_ack(BoomClient(), "123") is None


@pytest.mark.asyncio
async def test_drive_ack_progress_sequence_ends_with_almost_done() -> None:
    # #252 контракт: N случайных фраз прогресса → «Почти готово» (терминал, драйвер
    # завершается). Случайные — из пула _PROGRESS_PHRASES.
    from sreda.services.ack_messages import FINAL_PROGRESS_TEXT, _PROGRESS_PHRASES
    from sreda.services.telegram_inbound import _drive_ack_progress

    edits: list[str] = []

    class FakeClient:
        async def edit_message_text(self, **kwargs):  # noqa: ANN003
            edits.append(kwargs["text"])
            return {}

    await _drive_ack_progress(
        FakeClient(), "123", 42, step_seconds=0.001, max_random_edits=2,
    )
    assert len(edits) == 3                       # 2 рандома + «Почти готово»
    assert edits[-1] == FINAL_PROGRESS_TEXT      # терминал
    assert all(e in _PROGRESS_PHRASES for e in edits[:2])  # рандом из пула прогресса


@pytest.mark.asyncio
async def test_drive_ack_progress_edit_failure_does_not_raise() -> None:
    # Сбой правки ак best-effort — драйвер не падает, доходит до терминала.
    from sreda.services.ack_messages import FINAL_PROGRESS_TEXT
    from sreda.services.telegram_inbound import _drive_ack_progress

    seen: list[str] = []

    class FlakyClient:
        async def edit_message_text(self, **kwargs):  # noqa: ANN003
            seen.append(kwargs["text"])
            raise RuntimeError("edit failed")

    await _drive_ack_progress(
        FlakyClient(), "123", 42, step_seconds=0.001, max_random_edits=1,
    )
    assert seen[-1] == FINAL_PROGRESS_TEXT        # дошёл до терминала несмотря на сбои
