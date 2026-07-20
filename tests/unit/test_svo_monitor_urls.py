"""#370 — svo_monitor user-facing Telegram URLs on telegram.me (t.me DNS-dead).

Lock-in: preview-scrape URL и alert-ссылка строятся через централизованный
TELEGRAM_WEB_DOMAIN и указывают на telegram.me, не на битый короткий хост t.me.
"""

from __future__ import annotations

import asyncio

from sreda.config.constants import TELEGRAM_WEB_DOMAIN
from sreda.workers import svo_monitor


# HTML одного matching-поста: keyword'ы «введены … ограничения» + «ковёр»
# (см. _KEYWORD_REGEXES / _MESSAGE_RE в svo_monitor).
_MATCHING_HTML = (
    '<div data-post="svo_online/6707">'
    '<div class="tgme_widget_message_text js-message_text">'
    'Введены временные ограничения. Сигнал «ковёр».'
    "</div></div>"
)


def test_domain_constant_is_telegram_me():
    assert TELEGRAM_WEB_DOMAIN == "telegram.me"


def test_preview_url_uses_telegram_web_domain():
    # #370: preview-скрейпинг ломается на DNS битого t.me → telegram.me.
    assert svo_monitor._PREVIEW_URL == f"https://{TELEGRAM_WEB_DOMAIN}/s/svo_online"
    assert "//t.me/" not in svo_monitor._PREVIEW_URL


def test_tick_alert_body_links_to_telegram_web_domain(monkeypatch):
    """#370: реально прогнать _tick и убедиться, что alert-ссылка «Источник»
    в теле уходит на telegram.me, а не на битый t.me."""
    captured: dict[str, object] = {}

    async def _fake_fetch() -> str:
        return _MATCHING_HTML

    def _fake_read_last() -> str:
        return ""  # пост новый → алерт сработает

    def _fake_write(post_id: str) -> None:  # noqa: ARG001 — не пишем в БД в тесте
        pass

    def _fake_send_admin_alert(**kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(svo_monitor, "_fetch_preview", _fake_fetch)
    monkeypatch.setattr(svo_monitor, "_read_last_alerted", _fake_read_last)
    monkeypatch.setattr(svo_monitor, "_write_last_alerted", _fake_write)
    monkeypatch.setattr(svo_monitor, "send_admin_alert", _fake_send_admin_alert)

    asyncio.run(svo_monitor._tick())

    body = captured.get("body")
    assert body is not None, "alert не был отправлен — _tick не сматчил пост"
    assert "https://telegram.me/svo_online/6707" in body
    assert "//t.me/" not in body
