"""Одноразовая рассылка нового pending-приветствия существующим юзерам.

ЦЕЛЬ. Все одобренные тенанты (approved до 2026-04-28) никогда не
видели pending-цепочки из 11 сообщений (intro → 10 шагов → closing).
Шлём им:

  1. Сообщение «от Бориса» (один раз, текст ниже).
  2. Через ~3 минуты — `pending_bot._INTRO` с кнопкой «🎙️ Голос →».
  3. Дальше юзер сам тапает кнопки тура — webhook маршрутизирует
     `pb:<branch>` через `_handle_callback` (см. telegram_bot.py).

ИСПОЛЬЗОВАНИЕ.

    # Тест на одном юзере (по telegram_account_id):
    python -m scripts.broadcast_welcome_v2 --only 352612382

    # Только Boris-сообщение, без INTRO (для разделения шагов):
    python -m scripts.broadcast_welcome_v2 --only 352612382 --boris-only

    # Только INTRO (если уже отправил Boris отдельно):
    python -m scripts.broadcast_welcome_v2 --only 352612382 --intro-only

    # Dry-run — печатает кому что отправил бы, но не шлёт:
    python -m scripts.broadcast_welcome_v2 --dry-run

    # Реальная рассылка всем одобренным (без фильтра):
    python -m scripts.broadcast_welcome_v2 --confirm

ОДНОРАЗОВОСТЬ. Скрипт идемпотентность В БД не пишет — это
костыль. Если запустить повторно без --only — улетит дважды. Поэтому
для группового запуска обязателен --confirm + ручная проверка списка
в --dry-run перед этим.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime

from sqlalchemy.orm import Session

from sreda.config.settings import get_settings
from sreda.db.models.core import Tenant, User
from sreda.db.session import get_db_session
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.services import pending_bot


logger = logging.getLogger("broadcast_welcome_v2")


# Текст «от Бориса» — отправляется первым. Без эмодзи и Markdown
# (Telegram не рендерит). Опечатку «famaly» → «family» юзер сам
# подтвердил оставить как есть.
_BORIS_TEXT = (
    "Привет. Это Борис. Мини тест на friends&famaly показал что "
    "многим не очень понятно что такое Среда и как ее можно "
    "использовать. Я приготовил несколько приветственных сообщений "
    "в которых Среда рассказывает что она такое."
)


def _ts() -> str:
    """Timestamp для логов согласно глобальной директиве «логи без
    даты бессмысленны»."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    print(f"{_ts()} {msg}", flush=True)


def _list_targets(
    session: Session, *, only: str | None
) -> list[tuple[str, str, str]]:
    """Returns list of (tenant_id, user_id, telegram_account_id_decrypted)
    для всех approved тенантов (или одного, если --only)."""
    rows: list[tuple[str, str, str]] = []
    q = (
        session.query(Tenant, User)
        .join(User, User.tenant_id == Tenant.id)
        .filter(Tenant.approved_at.isnot(None))
    )
    for tenant, user in q.all():
        # telegram_account_id шифруется EncryptedString — на чтение
        # SQLAlchemy уже отдаёт plaintext.
        chat_id = (user.telegram_account_id or "").strip()
        if not chat_id:
            continue
        if only is not None and chat_id != only:
            continue
        rows.append((tenant.id, user.id, chat_id))
    return rows


async def _send_boris(client: TelegramClient, chat_id: str) -> None:
    await client.send_message(chat_id=chat_id, text=_BORIS_TEXT)


async def _send_intro(client: TelegramClient, chat_id: str) -> None:
    intro = pending_bot._BRANCHES["intro"]
    await client.send_message(
        chat_id=chat_id,
        text=intro.text,
        reply_markup=pending_bot.build_inline_keyboard(intro),
    )


async def _broadcast_one(
    client: TelegramClient,
    chat_id: str,
    *,
    boris: bool,
    intro: bool,
    sleep_seconds: int,
    dry_run: bool,
) -> None:
    if boris:
        if dry_run:
            _log(f"[dry-run] would send Boris → chat_id={chat_id}")
        else:
            try:
                await _send_boris(client, chat_id)
                _log(f"sent Boris → chat_id={chat_id}")
            except TelegramDeliveryError as exc:
                _log(f"FAILED Boris → chat_id={chat_id}: {exc}")
                return
    if boris and intro and not dry_run and sleep_seconds > 0:
        _log(f"sleeping {sleep_seconds}s before INTRO for chat_id={chat_id}")
        await asyncio.sleep(sleep_seconds)
    if intro:
        if dry_run:
            _log(f"[dry-run] would send INTRO → chat_id={chat_id}")
        else:
            try:
                await _send_intro(client, chat_id)
                _log(f"sent INTRO → chat_id={chat_id}")
            except TelegramDeliveryError as exc:
                _log(f"FAILED INTRO → chat_id={chat_id}: {exc}")


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.telegram_bot_token:
        _log("ERROR: SREDA_TELEGRAM_BOT_TOKEN not set; cannot send")
        return 2

    client = TelegramClient(settings.telegram_bot_token)

    # Open one session for the whole pass — list, then send.
    session_gen = get_db_session()
    session = next(session_gen)
    try:
        targets = _list_targets(session, only=args.only)
    finally:
        try:
            next(session_gen)
        except StopIteration:
            pass

    if not targets:
        if args.only:
            _log(f"no approved tenant matches telegram_account_id={args.only!r}")
        else:
            _log("no approved tenants found")
        return 1

    _log(f"targets: {len(targets)}")
    for tenant_id, user_id, chat_id in targets:
        _log(f"  tenant={tenant_id} user={user_id} chat_id={chat_id}")

    if not args.only and not args.confirm:
        _log(
            "REFUSING to broadcast to multiple users without --confirm. "
            "Re-run with --dry-run first to see who, then add --confirm."
        )
        return 3

    boris = not args.intro_only
    intro = not args.boris_only
    if not boris and not intro:
        _log("nothing to do (--boris-only and --intro-only mutually exclusive)")
        return 4

    sleep_seconds = 0 if args.no_sleep else args.sleep_seconds

    for _, _, chat_id in targets:
        await _broadcast_one(
            client,
            chat_id,
            boris=boris,
            intro=intro,
            sleep_seconds=sleep_seconds,
            dry_run=args.dry_run,
        )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--only", default=None,
        help="отправить только этому telegram_account_id (рекомендуется для теста)",
    )
    parser.add_argument(
        "--boris-only", action="store_true",
        help="отправить только сообщение от Бориса (без INTRO)",
    )
    parser.add_argument(
        "--intro-only", action="store_true",
        help="отправить только INTRO (Boris не слать)",
    )
    parser.add_argument(
        "--sleep-seconds", type=int, default=180,
        help="пауза между Boris и INTRO в секундах (default: 180 = 3 мин)",
    )
    parser.add_argument(
        "--no-sleep", action="store_true",
        help="не делать паузу между Boris и INTRO (для отладки)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="не слать ничего, только показать что отправил бы",
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="подтверждение для рассылки нескольким юзерам сразу",
    )
    args = parser.parse_args()

    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
