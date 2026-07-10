#!/usr/bin/env python3
"""Пост-деплойный сквозной smoke онбординга (мера B пост-мортема vex-assistant#331, #334).

Гонит НАСТОЯЩИЙ путь брэнд-нового юзера через ``handle_telegram_update`` (онбординг → первый ход →
ответ) для указанного бота/тира. Telegram-клиент застаблен (в сеть не шлём). Тест-тенант удаляется
фикспойнт-FK-чисткой. Exit 0 = онбординг проходит, exit 1 = крэш/аномалия (для гейтинга деплоя).

Мотив: #331 — крэш онбординга free-тира лендинга не видела канарейка (она на основном боте, безлимит).
Slepoe пятно закрывается прогоном по КАЖДОМУ тиру.

Запуск на проде (под окружением сервиса):
    sudo -u sreda /opt/sreda/.venv/bin/python /opt/sreda/scripts/onboard_smoke.py --bot-key sreda_home
    sudo -u sreda /opt/sreda/.venv/bin/python /opt/sreda/scripts/onboard_smoke.py --bot-key sreda

Каналы: telegram (sreda_home / sreda). MAX (max_inbound) — отдельный обработчик, TODO (#334 фаза 2).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

# Заведомо синтетические chat-id по тирам (не пересекаются с реальными юзерами).
_SMOKE_CHAT_BY_BOT = {
    "sreda_home": 990000001,
    "sreda": 990000002,
}


class _FakeTelegramClient:
    """Записывает «отправки», в сеть не ходит — smoke не шлёт реальным chat_id."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id=None, text=None, **_kw):  # noqa: ANN001
        self.sent.append(str(text)[:160])
        return 999001  # message_id (используется как int дальше по коду)

    async def edit_message_text(self, *_a, **_kw):
        return None

    async def delete_message(self, *_a, **_kw):
        return None

    async def answer_callback_query(self, *_a, **_kw):
        return None


def _update(update_id: int, chat: int, txt: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time()),
            "chat": {"id": chat, "type": "private"},
            "from": {"id": chat, "is_bot": False, "first_name": "OnboardSmoke"},
            "text": txt,
        },
    }


async def _await_detached_tasks(timeout_s: float = 40.0) -> None:
    """Ход спавнится ``asyncio.create_task`` (long-poll путь) — ждём завершения фоновых задач."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pending = [t for t in asyncio.all_tasks()
                   if t is not asyncio.current_task() and not t.done()]
        if not pending:
            return
        await asyncio.wait(pending, timeout=0.5)


def _fixpoint_cleanup(tenant_id: str) -> bool:
    """Удалить все строки тенанта FK-безопасно: крутим удаление tenant_id-таблиц, пока идёт прогресс
    (граф FK произвольной глубины: workspaces←secure_records←outbox/inbound и т.п.), потом tenants."""
    from sqlalchemy import text
    from sreda.db.session import get_engine
    eng = get_engine()
    with eng.connect() as conn:
        tbls = [r[0] for r in conn.execute(text(
            "SELECT table_name FROM information_schema.columns "
            "WHERE column_name = 'tenant_id' AND table_schema = 'public'")).all()]
    for _ in range(10):
        progress = 0
        for tbl in tbls:
            try:
                with eng.begin() as conn:
                    progress += conn.execute(
                        text(f'DELETE FROM "{tbl}" WHERE tenant_id = :t'), {"t": tenant_id}).rowcount
            except Exception:  # noqa: BLE001 — таблица ещё под FK, добьём на след. проходе
                pass
        if progress == 0:
            break
    with eng.begin() as conn:
        try:
            conn.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
        except Exception:  # noqa: BLE001
            pass
    with eng.connect() as conn:
        return conn.execute(text("SELECT 1 FROM tenants WHERE id = :t"), {"t": tenant_id}).first() is None


async def run_smoke(bot_key: str) -> bool:
    chat = _SMOKE_CHAT_BY_BOT.get(bot_key)
    if chat is None:
        print(f"[smoke] неизвестный bot_key={bot_key!r}; доступны: {list(_SMOKE_CHAT_BY_BOT)}")
        return False
    tenant_id = f"tenant_tg_{chat}"

    from sreda.services import telegram_inbound as TI
    from sreda.db.session import get_session_factory
    from sqlalchemy import text

    fake = _FakeTelegramClient()
    # предочистка (если прошлый прогон не добил) + подмена клиента
    _fixpoint_cleanup(tenant_id)
    TI.telegram_client_for = lambda *_a, **_k: fake  # type: ignore[assignment]

    print(f"[smoke] bot_key={bot_key} tenant={tenant_id}")
    ok = True
    try:
        # 1) /start — онбординг (auto-approve + welcome)
        await TI.handle_telegram_update(_update(970000001, chat, "/start"), bot_key=bot_key)
        await _await_detached_tasks()
        with get_session_factory()() as s:
            created = s.execute(text("SELECT 1 FROM tenants WHERE id = :t"), {"t": tenant_id}).first()
            sub = s.execute(text("SELECT status FROM tenant_subscriptions WHERE tenant_id = :t"),
                            {"t": tenant_id}).first()
        print(f"  [/start] тенант={bool(created)} подписка={sub[0] if sub else None}")
        ok = ok and bool(created)

        # 2) первый содержательный ход — крэш-путь #331 (quota) + полный ReAct
        await TI.handle_telegram_update(_update(970000002, chat, "привет"), bot_key=bot_key)
        await _await_detached_tasks()
        from sreda.db.models.react_trace import ReactTurnTrace
        with get_session_factory()() as s:
            rows = (s.query(ReactTurnTrace).filter(ReactTurnTrace.tenant_id == tenant_id).all())
            n_done = sum(1 for r in rows if r.status == "done")
            n_err = sum(1 for r in rows if r.status == "error")
        print(f"  [ход] трейс: done={n_done} error={n_err}; ответов={len(fake.sent)}")
        ok = ok and n_err == 0 and (n_done >= 1 or len(fake.sent) >= 1)
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"  [smoke] ❌ КРЭШ: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        ok = False
    finally:
        cleaned = _fixpoint_cleanup(tenant_id)
        print(f"  [cleanup] тест-тенант удалён: {cleaned}")
        ok = ok and cleaned

    print(f"[smoke] {bot_key}: {'PASS ✅' if ok else 'FAIL ❌'}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot-key", required=True, help="sreda_home (free-лендинг) | sreda (основной)")
    ap.add_argument("--env-file", default="/etc/sreda/.env",
                    help="файл окружения с DSN/секретами (по умолчанию прод). Пропускается, если нет.")
    args = ap.parse_args()
    # standalone-прогон (не под systemd) → подгружаем env сервиса; уже выставленное окружение НЕ трогаем.
    try:
        import os
        from pathlib import Path
        if args.env_file and Path(args.env_file).exists() and not os.environ.get("SREDA_DATABASE_URL"):
            from dotenv import load_dotenv
            load_dotenv(args.env_file)
    except Exception:  # noqa: BLE001 — под systemd env уже в процессе, dotenv не обязателен
        pass
    return 0 if asyncio.run(run_smoke(args.bot_key)) else 1


if __name__ == "__main__":
    sys.exit(main())
