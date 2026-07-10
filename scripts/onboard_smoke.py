#!/usr/bin/env python3
"""Пост-деплойный сквозной smoke онбординга (мера B пост-мортема vex-assistant#331, #334).

Гонит НАСТОЯЩИЙ путь брэнд-нового юзера через ``handle_telegram_update`` (онбординг → первый ход →
ответ) для указанного бота/тира, чтобы стык деплоёв ловила машина по КАЖДОМУ тиру (#331 — крэш free-тира
лендинга был невидим канарейке основного бота).

БЕЗОПАСНОСТЬ ДАННЫХ (ревью #334 R1→R3, критично — скрипт удаляет строки на ПРОДЕ):
- Синтетический id — ОТРИЦАТЕЛЬНЫЙ. Telegram НИКОГДА не выдаёт отрицательный id private-юзеру
  (отрицательные = группы/каналы). → тенант ``tenant_tg_<neg>`` ФИЗИЧЕСКИ не может совпасть с реальным
  private-аккаунтом. TOCTOU «проверил-нет → создал → а это реальный» исчезает airtight (C1/C3).
- Второй слой — verify-before-destroy: удаляем ТОЛЬКО тенант с нашим маркером в имени (``_SENTINEL``).
  Тенант с этим id БЕЗ маркера → ABORT, ничего не трогаем (fail-closed).
- update_id тоже ОТРИЦАТЕЛЬНЫЙ и уникальный по времени → не сталкивается с namespace реальных
  (положительных, растущих) Telegram update_id в dedup-ключе (channel, bot_key, external_update_id) (C3).
- Параллельные прогоны сериализованы pg advisory-lock по id тенанта: второй прогон → ABORT, не лезет в
  живой тенант первого (M3).
- admin-алерт (react_loop → send_admin_alert + сама egress-граница _post_*_sync) застаблен: smoke гонит
  крэш-класс, иначе зашлёт реальную тревогу оператору на каждом деплое (M1).
- signup_attempts (rate-limit 3/24ч, без tenant_id) чистятся по (channel, хеш источника) с проверкой
  остатка → нет ложного FAIL на 4-м прогоне и нет утечки (M4).
- Чистка: fixpoint по tenant_id-таблицам + schema-safe FK-граф на public.tenants(id); сбой delete
  tenants и остаток signup НЕ проглатываются — репортим (M2).

Здоровье хода (не «прошёл ли», а «здоров ли»):
- краш = trace остался ``status=in_progress`` (потерян finish-хук — ровно класс #331);
- деградация = ``outcome`` из {tool_error, llm_error, timeout, max_iter, fallback_used, safe_reply,
  policy_blocked}; здоровый онбординг-ход = ``done``/``outcome=ok``.

Exit: 0 — онбординг здоров и чисто; 1 — онбординг сломан (блок деплоя); 2 — abort (id занят / параллельный
прогон) ИЛИ онбординг ок, но чистка оставила остаток (предупредить, не путать со сломанным онбордингом).

Запуск на проде:
    sudo -u sreda /opt/sreda/.venv/bin/python /opt/sreda/scripts/onboard_smoke.py --bot-key sreda_home
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import time

# ОТРИЦАТЕЛЬНЫЙ id: реальному private-юзеру Telegram такой не выдаёт никогда → коллизия невозможна.
_SMOKE_CHAT_BY_BOT = {
    "sreda_home": -8_999_990_001,
    "sreda": -8_999_990_002,
}
_SENTINEL = "SMOKE-SYNTH-DELETE-OK"  # маркер в имени тенанта: удаляем ТОЛЬКО помеченное
# исходы ReactTurnTrace, означающие деградацию хода (ok = здоровый). policy_blocked онбординг не ждёт.
_DEGRADED_OUTCOMES = {
    "tool_error", "llm_error", "timeout", "max_iter", "fallback_used", "safe_reply", "policy_blocked",
}


class _FakeTelegramClient:
    """Записывает «отправки», в сеть не ходит. Форма ответа — как у настоящего клиента (dict с result)."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id=None, text=None, **_kw):  # noqa: ANN001
        self.sent.append(str(text)[:160])
        return {"ok": True, "result": {"message_id": 999001, "date": int(time.time())}}

    async def edit_message_text(self, *_a, **_kw):
        return {"ok": True, "result": {"message_id": 999001}}

    async def delete_message(self, *_a, **_kw):
        return {"ok": True}

    async def answer_callback_query(self, *_a, **_kw):
        return {"ok": True}


def _update(update_id: int, chat: int, txt: str) -> dict:
    return {"update_id": update_id,
            "message": {"message_id": update_id, "date": int(time.time()),
                        "chat": {"id": chat, "type": "private"},
                        "from": {"id": chat, "is_bot": False, "first_name": "OnboardSmoke"},
                        "text": txt}}


def _lock_key(tenant_id: str) -> int:
    """Детерминированный signed-bigint ключ для pg_advisory_lock (стабилен между процессами)."""
    return int.from_bytes(hashlib.sha256(tenant_id.encode()).digest()[:8], "big", signed=True)


def _is_pg(eng) -> bool:  # noqa: ANN001
    return eng.dialect.name == "postgresql"


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
    """Удалить тенант, ТОЛЬКО если он помечен _SENTINEL (иначе реальный → не трогаем).

    Возврат (полностью_чисто, why): полностью_чисто = тенант ушёл И остатка signup нет.
    """
    from sqlalchemy import text
    from sreda.db.session import get_engine
    eng = get_engine()
    with eng.connect() as c:
        name = _tenant_name(c, tenant_id)

    tenant_present = name is not None
    if tenant_present and _SENTINEL not in (name or ""):
        return False, f"ОТКАЗ: тенант {tenant_id} без маркера (реальный?) — НЕ удаляю"

    if tenant_present:
        # дети: tenant_id-колонки + schema-safe FK-граф на public.tenants(id)
        with eng.connect() as c:
            tid_tbls = [r[0] for r in c.execute(text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE column_name = 'tenant_id' AND table_schema = 'public'")).all()]
            fk_children = c.execute(text("""
                SELECT tc.table_name, kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_catalog = kcu.constraint_catalog
                 AND tc.constraint_schema = kcu.constraint_schema
                 AND tc.constraint_name = kcu.constraint_name
                JOIN information_schema.constraint_column_usage ccu
                  ON tc.constraint_catalog = ccu.constraint_catalog
                 AND tc.constraint_schema = ccu.constraint_schema
                 AND tc.constraint_name = ccu.constraint_name
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_schema = 'public'
                  AND ccu.table_schema = 'public'
                  AND ccu.table_name = 'tenants'
                  AND ccu.column_name = 'id'
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

    # signup_attempts (нет tenant_id) — по (channel, хеш источника). Чистим ВСЕГДА (даже если тенанта
    # нет: rate-limit-строки живут независимо), с проверкой остатка (M4).
    signup_leak = ""
    try:
        from sreda.services.signup_abuse import hmac_signup_source
        h = hmac_signup_source(str(chat))
        with eng.begin() as c:
            c.execute(text("DELETE FROM signup_attempts WHERE channel = 'telegram' "
                           "AND source_id_hash = :h"), {"h": h})
        with eng.connect() as c:
            left = c.execute(text("SELECT count(*) FROM signup_attempts WHERE channel = 'telegram' "
                                  "AND source_id_hash = :h"), {"h": h}).scalar()
        if left:
            signup_leak = f"; signup-остаток={left}"
    except Exception as e:  # noqa: BLE001
        signup_leak = f"; signup-чистка сбой: {type(e).__name__}"

    if not tenant_present:
        return (not signup_leak), ("не существует" + signup_leak)

    # tenants последним — сбой НЕ глотаем (M2: диагностируемо)
    blocker = ""
    try:
        with eng.begin() as c:
            c.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tenant_id})
    except Exception as e:  # noqa: BLE001
        blocker = f"delete tenants заблокирован: {type(e).__name__}: {str(e)[:120]}"
    with eng.connect() as c:
        gone = c.execute(text("SELECT 1 FROM tenants WHERE id = :t"), {"t": tenant_id}).first() is None
    return (gone and not signup_leak), ((blocker or "ok") + signup_leak)


async def run_smoke(bot_key: str) -> int:
    chat = _SMOKE_CHAT_BY_BOT.get(bot_key)
    if chat is None:
        print(f"[smoke] неизвестный bot_key={bot_key!r}; доступны: {list(_SMOKE_CHAT_BY_BOT)}")
        return 1
    tenant_id = f"tenant_tg_{chat}"

    from sreda.services import telegram_inbound as TI
    from sreda.services import admin_alerts as AA
    from sreda.db.session import get_engine, get_session_factory
    from sqlalchemy import text

    # M3: сериализуем параллельные прогоны того же тира — держим advisory-lock на ОТДЕЛЬНОМ соединении
    # весь прогон. Второй прогон не возьмёт лок → ABORT (не тронет живой тенант первого). Только PG.
    eng = get_engine()
    lock_conn = None
    if _is_pg(eng):
        lock_conn = eng.connect()
        locked = lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                   {"k": _lock_key(tenant_id)}).scalar()
        if not locked:
            lock_conn.close()
            print(f"[smoke] ⛔ ABORT: параллельный прогон {tenant_id} уже идёт — не вмешиваюсь.")
            return 2

    fake = _FakeTelegramClient()
    TI.telegram_client_for = lambda *_a, **_k: fake  # type: ignore[assignment]
    # M1: нейтрализуем egress admin-алерта (свой httpx-поток в TG/MAX минует заглушку клиента) на всех
    # слоях: и функцию send_admin_alert (её импортируют в момент вызова), и саму egress-границу
    # _post_*_sync (belt против будущего рефактора с ранней привязкой). httpx в сеть не уходит.
    _alerts: list = []
    AA.send_admin_alert = lambda *a, **k: _alerts.append((a, k))  # type: ignore[assignment]
    AA._post_telegram_sync = lambda *a, **k: True  # type: ignore[assignment]
    AA._post_max_sync = lambda *a, **k: True  # type: ignore[assignment]

    # уникальные по времени ОТРИЦАТЕЛЬНЫЕ update_id: не в namespace реальных (растущих +) Telegram, и
    # два последовательных прогона не словят dedup друг друга (C3).
    uid = -(900_000_000 + int(time.time()) % 100_000_000)

    # verify-before-destroy: тенант с этим id уже есть?
    with get_session_factory()() as s:
        existing_name = _tenant_name(s, tenant_id)
    if existing_name is not None and _SENTINEL not in existing_name:
        print(f"[smoke] ⛔ ABORT: {tenant_id} существует БЕЗ маркера (реальный тенант?) — не трогаю. "
              f"Разберись вручную.")
        _release_lock(lock_conn, tenant_id)
        return 2
    if existing_name is not None:  # наш остаток от прошлого прогона → чистим ДО старта
        ok, why = _cleanup_synthetic(tenant_id, chat)
        print(f"[smoke] пред-остаток (маркирован) очищен: {ok} ({why})")
        if not ok:  # brand-new гарантии больше нет → не гоним, чтобы старый trace не дал ложный PASS
            print(f"[smoke] ⛔ ABORT: пред-чистка не завершилась — прогон не brand-new.")
            _release_lock(lock_conn, tenant_id)
            return 2

    print(f"[smoke] bot_key={bot_key} tenant={tenant_id}")
    onboard_ok = True
    try:
        await TI.handle_telegram_update(_update(uid, chat, "/start"), bot_key=bot_key)
        await _await_detached_tasks()
        with get_session_factory()() as s:
            created = s.execute(text("SELECT 1 FROM tenants WHERE id = :t"), {"t": tenant_id}).first()
            sub = s.execute(text("SELECT status FROM tenant_subscriptions WHERE tenant_id = :t"),
                            {"t": tenant_id}).first()
        print(f"  [/start] тенант={bool(created)} подписка={sub[0] if sub else None}")
        onboard_ok = onboard_ok and bool(created)
        if created:
            _mark_synthetic(tenant_id)  # пометить СРАЗУ после создания

        excs = await _run_turn(TI, uid - 1, chat, bot_key)
        from sreda.db.models.react_trace import ReactTurnTrace
        # здоровье хода: краш = остался in_progress (потерян finish-хук, класс #331); деградация =
        # outcome в _DEGRADED_OUTCOMES; здоровый = done/outcome=ok. Тенант brand-new → все его trace-строки
        # только от этого прогона (скоуп по tenant_id = скоуп по текущему ходу).
        with get_session_factory()() as s:
            rows = s.query(ReactTurnTrace).filter(ReactTurnTrace.tenant_id == tenant_id).all()
            n_ok = sum(1 for r in rows if r.status == "done" and (r.outcome or "") == "ok")
            n_stuck = sum(1 for r in rows if r.status == "in_progress")
            n_degraded = sum(1 for r in rows if (r.outcome or "") in _DEGRADED_OUTCOMES)
        print(f"  [ход] трейс: ok={n_ok} in_progress(краш)={n_stuck} degraded={n_degraded}; "
              f"ответов={len(fake.sent)}; admin-алертов подавлено={len(_alerts)}; фон-исключений={len(excs)}")
        onboard_ok = (onboard_ok and n_ok >= 1 and n_stuck == 0 and n_degraded == 0
                      and len(fake.sent) >= 1 and not excs)
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"  [smoke] ❌ КРЭШ: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        onboard_ok = False
    finally:
        try:
            cleaned, why = _cleanup_synthetic(tenant_id, chat)
        except Exception as e:  # noqa: BLE001 — сбой чистки = остаток, exit 2 (не путать со сломанным онбордингом)
            cleaned, why = False, f"чистка упала: {type(e).__name__}: {str(e)[:120]}"
        print(f"  [cleanup] тест-тенант удалён и чисто: {cleaned} ({why})")
        _release_lock(lock_conn, tenant_id)

    if not onboard_ok:
        print(f"[smoke] {bot_key}: FAIL ❌ (онбординг сломан)")
        return 1
    if not cleaned:
        print(f"[smoke] {bot_key}: онбординг OK, но чистка оставила остаток ⚠️ (нужно вмешательство)")
        return 2
    print(f"[smoke] {bot_key}: PASS ✅")
    return 0


def _release_lock(lock_conn, tenant_id: str) -> None:  # noqa: ANN001
    if lock_conn is None:
        return
    try:
        from sqlalchemy import text
        lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _lock_key(tenant_id)})
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            lock_conn.close()
        except Exception:  # noqa: BLE001
            pass


async def _run_turn(TI, update_id: int, chat: int, bot_key: str) -> list[BaseException]:
    await TI.handle_telegram_update(_update(update_id, chat, "привет"), bot_key=bot_key)
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
