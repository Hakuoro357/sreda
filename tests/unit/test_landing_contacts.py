"""#370 — home-landing contacts stub locks in telegram.me + correct home bot.

Статический deploy/sredaspace/legal/contacts.html не может импортировать
Python-константу, поэтому регрессию (возврат на битый t.me или устаревший
бот sreda01_bot) ловим чтением файла.
"""

from __future__ import annotations

from pathlib import Path

_CONTACTS = (
    Path(__file__).resolve().parents[2]
    / "deploy" / "sredaspace" / "legal" / "contacts.html"
)


def test_contacts_bot_link_on_telegram_me_home_bot():
    html = _CONTACTS.read_text(encoding="utf-8")
    # #370: домен на telegram.me, бот home-издания — @sreda_home_bot.
    assert 'href="https://telegram.me/sreda_home_bot"' in html
    assert "@sreda_home_bot" in html
    # Битый короткий хост и устаревший бот не должны остаться.
    assert "//t.me/" not in html
    assert "sreda01_bot" not in html
