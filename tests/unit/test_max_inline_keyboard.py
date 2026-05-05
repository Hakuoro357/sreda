"""Tests для render_max_inline_keyboard_attachment.

Конвертер TG ``inline_keyboard`` → MAX ``attachments[].payload.buttons``.
Per MAX docs /docs-api/objects/NewMessageBody:
- type: callback | link | request_contact | request_geo_location
- callback: payload required (TG callback_data)
- link: url required
"""

from __future__ import annotations

from sreda.integrations.max.client import render_max_inline_keyboard_attachment


def test_none_reply_markup_returns_none():
    assert render_max_inline_keyboard_attachment(None) is None


def test_empty_dict_returns_none():
    assert render_max_inline_keyboard_attachment({}) is None


def test_no_inline_keyboard_field_returns_none():
    assert render_max_inline_keyboard_attachment({"keyboard": [[]]}) is None


def test_empty_inline_keyboard_returns_none():
    assert render_max_inline_keyboard_attachment({"inline_keyboard": []}) is None


def test_single_callback_button():
    tg = {
        "inline_keyboard": [[
            {"text": "Готово ✅", "callback_data": "rem_done:abc"},
        ]],
    }
    result = render_max_inline_keyboard_attachment(tg)
    assert result == [{
        "type": "inline_keyboard",
        "payload": {
            "buttons": [[
                {"type": "callback", "text": "Готово ✅", "payload": "rem_done:abc"},
            ]],
        },
    }]


def test_single_link_button():
    tg = {
        "inline_keyboard": [[
            {"text": "Открыть", "url": "https://example.com"},
        ]],
    }
    result = render_max_inline_keyboard_attachment(tg)
    assert result == [{
        "type": "inline_keyboard",
        "payload": {
            "buttons": [[
                {"type": "link", "text": "Открыть", "url": "https://example.com"},
            ]],
        },
    }]


def test_two_buttons_in_row():
    tg = {
        "inline_keyboard": [[
            {"text": "Готово ✅", "callback_data": "rem_done:1"},
            {"text": "Отложить ⏰", "callback_data": "rem_snooze:1"},
        ]],
    }
    result = render_max_inline_keyboard_attachment(tg)
    assert result is not None
    buttons = result[0]["payload"]["buttons"]
    assert len(buttons) == 1  # один row
    assert len(buttons[0]) == 2  # два button'а
    assert buttons[0][0]["type"] == "callback"
    assert buttons[0][0]["payload"] == "rem_done:1"
    assert buttons[0][1]["payload"] == "rem_snooze:1"


def test_multi_row_keyboard():
    tg = {
        "inline_keyboard": [
            [{"text": "A", "callback_data": "a"}],
            [{"text": "B", "callback_data": "b"}],
        ],
    }
    result = render_max_inline_keyboard_attachment(tg)
    assert result is not None
    buttons = result[0]["payload"]["buttons"]
    assert len(buttons) == 2
    assert buttons[0][0]["text"] == "A"
    assert buttons[1][0]["text"] == "B"


def test_unsupported_button_type_skipped():
    """web_app / login_url / pay — нет MAX equivalent → silent skip."""
    tg = {
        "inline_keyboard": [[
            {"text": "Mini-App", "web_app": {"url": "https://miniapp"}},
            {"text": "Готово", "callback_data": "x"},
        ]],
    }
    result = render_max_inline_keyboard_attachment(tg)
    # web_app skip'ается, остаётся только callback
    assert result is not None
    buttons = result[0]["payload"]["buttons"]
    assert len(buttons[0]) == 1
    assert buttons[0][0]["type"] == "callback"


def test_button_without_text_or_action_skipped():
    tg = {
        "inline_keyboard": [[
            {"text": "", "callback_data": "x"},  # empty text
            {"text": "ok"},  # no action
            {"text": "Real", "callback_data": "real"},
        ]],
    }
    result = render_max_inline_keyboard_attachment(tg)
    assert result is not None
    buttons = result[0]["payload"]["buttons"]
    assert len(buttons[0]) == 1
    assert buttons[0][0]["payload"] == "real"


def test_all_buttons_unsupported_returns_none():
    """Если ВСЕ buttons unsupported (нет ни link ни callback) → None,
    text-only message без attachments."""
    tg = {
        "inline_keyboard": [[
            {"text": "Mini-App", "web_app": {"url": "x"}},
        ]],
    }
    assert render_max_inline_keyboard_attachment(tg) is None
