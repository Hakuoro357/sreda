"""SVO Шереметьево «ковёр» signal monitor.

Polls public Telegram channel @svo_online via t.me preview page,
matches exact phrase about temporary airspace restrictions, alerts
admin chat via ``send_admin_alert``.

Архитектурное решение: НЕ через TG bot API (бот не участник
канала и не может им стать — это чужой публичный канал) и НЕ через
MTProto/Telethon (требует user account + новой зависимости). Вместо
этого скрейпинг публичной preview страницы ``https://t.me/s/<channel>``
— простой HTML, никаких рейт-лимитов на наших объёмах (60s poll).

Запускается из ``run_job_loop_async`` как фоновый asyncio task,
рядом с ``run_health_check_loop``. Best-effort — exception в tick'е
swallowed + log, цикл не падает.

Tracking: последний alert'нутый ``data-post`` id хранится в
``runtime_config[svo_monitor_last_alerted_post]``. Повторно тот же
пост не алертим.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
from typing import Final

import httpx

from sreda.db.session import get_session_factory
from sreda.services import runtime_config as rc
from sreda.services.admin_alerts import send_admin_alert


logger = logging.getLogger(__name__)


_CHANNEL: Final = "svo_online"
_PREVIEW_URL: Final = f"https://t.me/s/{_CHANNEL}"

# Matching правила (2026-05-18 v3 update): только INITIAL объявления
# о ковре, без followup'ов. Требуем:
#   (1) упоминание «ковёр»/«ковер» (любой word stem)
#   (2) фразу «Введены [временные] ограничения» в одном предложении —
#       это инициирующий verb «введ-ены/-ено/-ена» (passive perfective),
#       а НЕ «введ-ением» (gerund / instrumental — followup pattern).
#
# Verified на истории канала:
#   post 6707 «Введены временные ограничения … ковёр» → match ✓
#   post 6708 «Продолжается действие … введением сигнала «ковер»» → skip ✓
#   post 6712 «Введены временные ограничения … ковёр» → match ✓
#
# `\bвведен[ыоа]\b` — strict word boundary: "введено"/"введена"/"введены"
# matches, но "введением" (продолжение через -ением) НЕ matches т.к. нет
# word boundary после "введен" внутри "введением".
_KEYWORD_REGEXES: Final = (
    re.compile(r"\bков(?:ё|е)р\w*\b", re.IGNORECASE),
    re.compile(
        r"\bвведен[ыоа]\b[^.]{0,40}\bограничен\w+\b",
        re.IGNORECASE,
    ),
)

# Извлекаем блоки сообщений: каждый блок имеет data-post id и
# tgme_widget_message_text payload. .*? + DOTALL чтобы поймать
# многострочный текст внутри text-div.
_MESSAGE_RE: Final = re.compile(
    r'data-post="(svo_online/\d+)".*?'
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL,
)
_TAG_STRIP_RE: Final = re.compile(r"<[^>]+>")

_RC_KEY: Final = "svo_monitor_last_alerted_post"
_POLL_SECONDS: Final = 60.0
_FETCH_TIMEOUT: Final = 10.0


async def _fetch_preview() -> str | None:
    """Получить HTML public preview страницы канала. None на любой ошибке."""
    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (sreda-svo-monitor)"},
        ) as client:
            response = await client.get(_PREVIEW_URL)
            response.raise_for_status()
            return response.text
    except Exception:
        # Любой network/HTTP error: лог + None → tick молча skip.
        logger.warning("svo_monitor: fetch failed", exc_info=True)
        return None


def _post_matches(text: str) -> bool:
    """True если все три keyword regex match'нулись в тексте поста."""
    return all(rx.search(text) for rx in _KEYWORD_REGEXES)


def _extract_matching_post_id(html: str) -> str | None:
    """Найти самый свежий пост с целевыми keyword'ами. Возвращает post_id или None.

    t.me preview возвращает посты в хронологическом порядке (старые
    сверху, новые снизу). Iterate всё и берём последний match — это
    будет newest matching post в текущем preview window.
    """
    last_match: str | None = None
    for m in _MESSAGE_RE.finditer(html):
        post_id, text_html = m.groups()
        # Удалить inline HTML теги (<br>, <a>, <b>, etc), затем
        # раскодировать сущности (&quot;, &laquo;, ...).
        text = _TAG_STRIP_RE.sub("", text_html)
        text = html_lib.unescape(text).strip()
        if _post_matches(text):
            last_match = post_id
    return last_match


def _read_last_alerted() -> str:
    """Считать последний alert'нутый post_id из runtime_config (sync)."""
    session = get_session_factory()()
    try:
        return rc.get_config(session, _RC_KEY) or ""
    finally:
        session.close()


def _write_last_alerted(post_id: str) -> None:
    """Записать post_id как alert'нутый (sync, commits внутри set_config)."""
    session = get_session_factory()()
    try:
        rc.set_config(session, _RC_KEY, post_id)
    finally:
        session.close()


async def _tick() -> None:
    """Один poll: fetch → match → alert если post новый."""
    html = await _fetch_preview()
    if html is None:
        return

    post_id = _extract_matching_post_id(html)
    if post_id is None:
        return  # фраза не найдена — норма (большинство тиков)

    # DB I/O в thread pool чтобы не блокировать event loop.
    last_alerted = await asyncio.to_thread(_read_last_alerted)
    if post_id == last_alerted:
        return  # уже алертили этот пост, пропускаем

    # Новый match — fire alert. send_admin_alert уже fire-and-forget
    # (внутри spawn'ит daemon thread), не блокируется на HTTP timeout.
    send_admin_alert(
        severity="INFO",
        title="✈️ SVO Шереметьево: сигнал «ковёр»",
        body=(
            f"Канал @{_CHANNEL} опубликовал сообщение содержащее "
            f"ключевые слова: ковёр + ограничения + введены.\n\n"
            f"Источник: https://t.me/{post_id}"
        ),
        dedupe_key=f"svo_monitor:{post_id}",
        extra_context={"post_id": post_id, "channel": _CHANNEL},
    )

    # Persist что мы уже alert'нули этот пост.
    await asyncio.to_thread(_write_last_alerted, post_id)
    logger.info("svo_monitor: alert sent for %s", post_id)


async def run_svo_monitor_loop() -> None:
    """Бесконечный цикл с poll'ом каждые ``_POLL_SECONDS`` секунд.

    Best-effort: исключение в tick'е swallowed + log, loop продолжается.
    """
    logger.info(
        "svo_monitor: started, polling %s every %.0fs",
        _PREVIEW_URL, _POLL_SECONDS,
    )
    while True:
        try:
            await _tick()
        except Exception:  # noqa: BLE001 — loop никогда не должен умирать
            logger.exception("svo_monitor: tick failed, continuing")
        await asyncio.sleep(_POLL_SECONDS)
