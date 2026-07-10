#!/usr/bin/env python3
"""Пост-деплойный сквозной smoke онбординга (мера B пост-мортема vex-assistant#331, #334).

Гонит НАСТОЯЩИЙ путь брэнд-нового юзера через ``handle_telegram_update`` (онбординг → первый ход →
ответ) для указанного бота/тира, чтобы стык деплоёв ловила машина по КАЖДОМУ тиру (#331 — крэш free-тира
лендинга был невидим канарейке основного бота).

БЕЗОПАСНОСТЬ ДАННЫХ (ревью #334 R1, критично — скрипт удаляет строки на ПРОДЕ):
- Синтетический id ОГРОМНЫЙ (далеко за текущим диапазоном Telegram) → крайне маловероятно совпасть с
  реальным аккаунтом. Но это лишь запасной слой.
- ГЛАВНЫЙ слой — verify-before-destroy: удаляем ТОЛЬКО тенант с нашим маркером в имени (``_SENTINEL``).
  Тенант с этим id БЕЗ маркера (= реальный юзер) → ABORT, ничего не трогаем (fail-closed). Реальные данные
  не удаляются НИКОГДА.
- admin-алерт (react_loop → send_admin_alert, свой httpx-поток в Telegram/MAX) застаблен: smoke гонит
  крэш-класс, на деградации иначе зашлёт реальную тревогу оператору на каждом деплое.
- signup_attempts (rate-limit 3/24ч, без tenant_id) чистятся по хешу источника → нет ложного FAIL на 4-м
  прогоне и нет утечки.
- Чистка: fixpoint по tenant_id-таблицам + FK-граф (колонки не всегда зовутся tenant_id); сбой delete
  tenants НЕ проглатывается — репортим, что заблокировало.

Exit: 0 — онбординг ок и чисто; 1 — онбординг сломан (блок деплоя); 2 — abort (id занят реальным тенантом)
ИЛИ онбординг ок, но чистка оставила остаток (предупредить, не путать со сломанным онбордингом).

Запуск на проде:
    sudo -u sreda /opt/sreda/.venv/bin/python /opt/sreda/scripts/onboard_smoke.py --bot-key sreda_home
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

# ОГРОМНЫЙ положительный id, далеко за текущим диапазоном Telegram (~8e9 в 2026) → практически не столкнётся
# с реальным аккаунтом. Настоящая гарантия — маркер (_SENTINEL), а не это значение.
_SMOKE_CHAT_BY_BOT = {
    "sreda_home": 8_999_990_001,
    "sreda": 8_999_990_002,
}
_SENTINEL = "SMOKE-SYNTH-DELETE-OK"  # маркер в имени тенанта: удаляем ТОЛЬКО помеченное


class _FakeTelegramClient:
    """Записывает «отправки», в сеть не ходит."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id=None, text=None, **_kw):  # noqa: ANN001
        self.sent.append(str(text)[:160])
        return 999001

    async def edit_message_text(self, *_a, **_kw):
        return None

    async def delete_message(self, *_a, **_kw):
        return None

    async def answer_callback_query(self, *_a, **_kw):
        return None


def _update(update_id: int, chat: int, txt: str) -> dict:
    return {"update_id": update_id,
            "message": {"message_id": update_id, "date": int(time.time()),
                        "chat": {"id": chat, "type": "private"},
                        "from": {"id": chat, "is_bot": False, "first_name": "OnboardSmoke"},
                        "text": txt}}


async def _await_detached_tasks(timeout_s: float = 40.0) -> list[BaseException]:
    """Ход спавнится create_task; ждём фоновые задачи и СОБИРАЕМ их исключения (иначе крэш хода
    не дойдёт до exit-кода — _process_approved_turn_locked глотает своё исключение)."""
    excs: list[BaseException] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pending = [t for t in asyncio.all_tasks()
                   if t is not asyncio.current_task() and not t.done()]
        if not pending:
            break
        done, _ = await asyncio.wait(pending, timeout=0.5)
        for t in done:
            e = t.exception() if not t.cancelled() else None
            if e is not None:
                excs.append(e)
    return excs


def _tenant_name(session, tenant_id: str) -> str | None:
    from sqlalchemy import text
    r = session.execute(text("SELECT name FROM tenants WHERE id = :t"), {"t": tenant_id}).first()
    return r[0] if r else None


def _mark_synthetic(tenant_id: str) -> None:
    """Пометить наш тенант маркером в имени — чистка удаляет ТОЛЬКО помеченное."""
    from sqlalchemy import text
    from sreda.db.session import get_engine
    with get_engine().begin() as c:
        c.execute(text("UPDATE tenants SET name = :n WHERE id = :t"),
                  {"n": f"{_SENTINEL} {tenant_id}", "t": tenant_id})


def _cleanup_synthetic(tenant_id: str, chat: int) -> tuple[bool, str]:
    """Удалить тенант, ТОЛЬКО если он помечен _SENTINEL (иначе реальный → не трогаем). Возврат (удалён, why)."""
    from sqlalchemy import text
    from sreda.db.session import get_engine
    eng = get_engine()
    with eng.connect() as c:
        name = _tenant_name(c, tenant_id)
    if name is None:
        return True, "не существует"
    if _SENTINEL not in (name or ""):
        return False, f"ОТКАЗ: тенант {tenant_id} без маркера (реальный?) — НЕ удаляю"
    # дети: tenant_id-колонки + реальный FK-граф на tenants
    with eng.connect() as c:
        tid_tbls = [r[0] for r in c.execute(text(
            "SELECT table_name FROM information_schema.columns "
            "WHERE column_name = 'tenant_id' AND table_schema = 'public'")).all()]
        fk_children = c.execute(text("""
            SELECT tc.table_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.constraint_column_usage ccu ON tc.constraint_name = ccu.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY' AND ccu.table_name = 'tenants'
        """)).all()
    for _ in range(10):
        progress = 0
        for tbl in tid_tbls:
            try:
                with eng.begin() as c:
                    progress += c.execute(text(f'DELETE FROM "{tbl}" WHERE tenant_id = :t'),
                                          {"t": tenant_id}).rowcount
            except Exception:  # noqa: BLE001 — под FK, добьём на след. проходе
                pass
        # FK-дети с НЕ-tenant_id колонкой (M2)
        for tbl, col in fk_children:
            if tbl == "tenants" or col == "tenant_id":
                continue
            try:
                with eng.begin() as c:
                    progress += c.execute(text(f'DELETE FROM "{tbl}" WHERE "{col}" = :t'),
                                          {"t": tenant_id}).rowcount
            except Exception:  # noqa: BLE001
                pass
        if progress == 0:
            break
    # signup_attempts (нет tenant_id) — по хешу источника (M4: снять rate-limit + утечку)
    try:
        from sreda.services.signup_abuse import hmac_signup_source
        with eng.begin() as c:
            c.execute(text("DELETE FROM signup_attempts WHERE source_id_hash = :h"),
                      {"h": hmac_signup_source(str(chat))})
    except Exception:  # noqa: BLE001
        pass
    # tenants последним — сбой НЕ глотаем (M2: диагностируемо)
    blocker = ""
    try:
        with eng.begin() as c:
            c.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
    except Exception as e:  # noqa: BLE001
        blocker = f"delete tenants заблокирован: {type(e).__name__}: {str(e)[:120]}"
    with eng.connect() as c:
        gone = c.execute(text("SELECT 1 FROM tenants WHERE id = :t"), {"t": tenant_id}).first() is None
    return gone, (blocker or "ok")


async def run_smoke(bot_key: str) -> int:
    chat = _SMOKE_CHAT_BY_BOT.get(bot_key)
    if chat is None:
        print(f"[smoke] неизвестный bot_key={bot_key!r}; доступны: {list(_SMOKE_CHAT_BY_BOT)}")
        return 1
    tenant_id = f"tenant_tg_{chat}"

    from sreda.services import telegram_inbound as TI
    from sreda.services import admin_alerts as AA
    from sreda.db.session import get_session_factory
    from sqlalchemy import text

    fake = _FakeTelegramClient()
    TI.telegram_client_for = lambda *_a, **_k: fake  # type: ignore[assignment]
    # M1: нейтрализуем egress admin-алерта (свой httpx-поток в TG/MAX минует заглушку клиента)
    _alerts: list = []
    AA.send_admin_alert = lambda *a, **k: _alerts.append((a, k))  # type: ignore[assignment]

    # verify-before-destroy: тенант с этим id уже есть?
    with get_session_factory()() as s:
        existing_name = _tenant_name(s, tenant_id)
    if existing_name is not None and _SENTINEL not in existing_name:
        print(f"[smoke] ⛔ ABORT: {tenant_id} существует БЕЗ маркера (реальный тенант?) — не трогаю. "
              f"Разберись вручную.")
        return 2
    if existing_name is not None:  # наш остаток от прошлого прогона → чистим
        ok, why = _cleanup_synthetic(tenant_id, chat)
        print(f"[smoke] пред-остаток (маркирован) очищен: {ok} ({why})")

    print(f"[smoke] bot_key={bot_key} tenant={tenant_id}")
    onboard_ok = True
    try:
        await TI.handle_telegram_update(_update(970000001, chat, "/start"), bot_key=bot_key)
        await _await_detached_tasks()
        with get_session_factory()() as s:
            created = s.execute(text("SELECT 1 FROM tenants WHERE id = :t"), {"t": tenant_id}).first()
            sub = s.execute(text("SELECT status FROM tenant_subscriptions WHERE tenant_id = :t"),
                            {"t": tenant_id}).first()
        print(f"  [/start] тенант={bool(created)} подписка={sub[0] if sub else None}")
        onboard_ok = onboard_ok and bool(created)
        if created:
            _mark_synthetic(tenant_id)  # пометить СРАЗУ после создания

        excs = await _run_turn(TI, chat, bot_key)
        from sreda.db.models.react_trace import ReactTurnTrace
        with get_session_factory()() as s:
            rows = s.query(ReactTurnTrace).filter(ReactTurnTrace.tenant_id == tenant_id).all()
            n_done = sum(1 for r in rows if r.status == "done")
            n_err = sum(1 for r in rows if r.status == "error")
        print(f"  [ход] трейс: done={n_done} error={n_err}; ответов={len(fake.sent)}; "
              f"admin-алертов подавлено={len(_alerts)}; фон-исключений={len(excs)}")
        # m1: требуем done>=1 И нет error И нет непойманных фон-исключений (не OR sent)
        onboard_ok = onboard_ok and n_done >= 1 and n_err == 0 and not excs
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"  [smoke] ❌ КРЭШ: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        onboard_ok = False
    finally:
        cleaned, why = _cleanup_synthetic(tenant_id, chat)
        print(f"  [cleanup] тест-тенант удалён: {cleaned} ({why})")

    if not onboard_ok:
        print(f"[smoke] {bot_key}: FAIL ❌ (онбординг сломан)")
        return 1
    if not cleaned:
        print(f"[smoke] {bot_key}: онбординг OK, но чистка оставила остаток ⚠️")
        return 2
    print(f"[smoke] {bot_key}: PASS ✅")
    return 0


async def _run_turn(TI, chat: int, bot_key: str) -> list[BaseException]:
    await TI.handle_telegram_update(_update(970000002, chat, "привет"), bot_key=bot_key)
    return await _await_detached_tasks()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot-key", required=True, help="sreda_home (free-лендинг) | sreda (основной)")
    ap.add_argument("--env-file", default="/etc/sreda/.env")
    args = ap.parse_args()
    try:
        from pathlib import Path
        if args.env_file and Path(args.env_file).exists() and not os.environ.get("SREDA_DATABASE_URL"):
            from dotenv import load_dotenv
            load_dotenv(args.env_file)
    except Exception:  # noqa: BLE001
        pass
    return asyncio.run(run_smoke(args.bot_key))


if __name__ == "__main__":
    sys.exit(main())
