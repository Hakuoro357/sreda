#!/usr/bin/env python3
"""Пост-деплойный сквозной smoke онбординга (мера B пост-мортема vex-assistant#331, #334).

Гонит НАСТОЯЩИЙ путь брэнд-нового юзера через ``handle_telegram_update`` (онбординг → первый ход →
ответ) для указанного бота/тира, чтобы стык деплоёв ловила машина по КАЖДОМУ тиру (#331 — крэш free-тира
лендинга был невидим канарейке основного бота).

БЕЗОПАСНОСТЬ ДАННЫХ (ревью #334 R1→R4; критично — скрипт удаляет строки на ПРОДЕ).
Гарантия «не тронуть реального юзера» держится на ТРЁХ независимых слоях, а НЕ на выборе id:
  1. Ownership-пруф по ТВЁРДОМУ контракту update_id. Telegram update_id ВСЕГДА неотрицательный и
     монотонно растёт (док Bot API); наши синтетические update_id — ОТРИЦАТЕЛЬНЫЕ. «Тенант наш» ⟺ у него
     НЕТ ни одного inbound_messages с неотрицательным external_update_id. Любая примесь реальной
     активности (положительный update_id) → ABORT, не помечаем и не удаляем. Это, а не знак chat_id,
     и есть airtight-инвариант (отрицательный chat_id сам по себе НЕ невозможен — у групп/каналов он
     отрицательный; на это не опираемся).
  2. verify-before-destroy + маркер: удаляем ТОЛЬКО тенант с нашим маркером в имени (``_SENTINEL``);
     финальный DELETE атомарно guardʼится ``AND name LIKE marker`` (если имя перестало быть маркированным
     между проверкой и удалением — DELETE это no-op). Немаркированный тенант по нашему id → ABORT.
  3. advisory-lock по id тенанта сериализует параллельные прогоны (второй → ABORT, не лезет в живой
     тенант первого).
Синтетический id всё же выбран вне обычных диапазонов Telegram (~-9e9: за 32-битными basic-группами и
до супергрупп -1e12) как доп. подушка, но это НЕ несущий слой.

- admin-алерт (react_loop → send_admin_alert + сама egress-граница _post_*_sync) застаблен: smoke гонит
  крэш-класс, иначе зашлёт реальную тревогу оператору на каждом деплое.
- signup_attempts (rate-limit 3/24ч, без tenant_id) чистятся по (channel='telegram', хеш источника) с
  проверкой остатка → нет ложного FAIL на 4-м прогоне и нет утечки.

Здоровье хода (не «прошёл ли», а «здоров ли»):
- краш = trace остался ``status=in_progress`` (потерян finish-хук — ровно класс #331);
- деградация = ``outcome`` из {tool_error, safe_reply} ИЛИ текст ответа = маркер штопора
  ``_MAX_STEPS_MARKER`` (штопор пишется как done/outcome=ok, детект — по тексту, не по outcome);
- fallback_used НЕ считаем деградацией (ход ответил через резервную модель — это успех);
- здоровый онбординг-ход = свежий ``done``/``outcome=ok`` + доставленный НОВЫЙ ответ.

Exit: 0 — онбординг здоров и чисто; 1 — онбординг сломан (блок деплоя); 2 — abort (id занят реальной
активностью / параллельный прогон / трейс выключен — не могу оценить) ИЛИ онбординг ок, но чистка
оставила остаток (предупредить, не путать со сломанным онбордингом).

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
from datetime import datetime, timezone

# id вне обычных диапазонов Telegram (доп. подушка; несущий инвариант — ownership по update_id, см. шапку).
# Значение подобрано так, чтобы run_id "react:telegram:tenant_tg_<id>:in_<24hex>" влезал в varchar(64).
_SMOKE_CHAT_BY_BOT = {
    "sreda_home": -8_999_990_001,
    "sreda": -8_999_990_002,
}
_SENTINEL = "SMOKE-SYNTH-DELETE-OK"  # маркер в имени тенанта: удаляем ТОЛЬКО помеченное
# ожидаемый статус подписки после онбординга per-bot (gate проверяет тир, не только факт создания)
_EXPECTED_SUB_STATUS = {"sreda_home": "active", "sreda": "active"}
# исходы ReactTurnTrace, означающие деградацию хода. fallback_used НЕ здесь (ход ответил через резерв =
# успех, иначе транзиентный фолбэк LLM ложно валил бы деплой). Штопор ловится отдельно по тексту.
_DEGRADED_OUTCOMES = {"tool_error", "safe_reply"}


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


def _has_foreign_activity(eng, tenant_id: str) -> bool:  # noqa: ANN001
    """Слой 1: есть ли у тенанта inbound с НЕотрицательным external_update_id (= реальная активность).

    Наши update_id отрицательные; реальные Telegram — всегда положительные (контракт Bot API). Любой
    inbound не из нашего отрицательного namespace → тенант делит id с реальным чатом → НЕ наш.
    NULL external_update_id тоже считаем чужим (безопаснее: не помечать/не удалять при сомнении).
    """
    from sqlalchemy import text
    with eng.connect() as c:
        n = c.execute(text(
            "SELECT count(*) FROM inbound_messages WHERE tenant_id = :t "
            "AND (external_update_id IS NULL OR external_update_id NOT LIKE '-%')"),
            {"t": tenant_id}).scalar()
    return bool(n)


async def _await_detached_tasks(timeout_s: float = 40.0) -> list[BaseException]:
    """Ход спавнится create_task; ждём фоновые задачи и СОБИРАЕМ их исключения (belt — авторитетный
    сигнал краша это trace status=in_progress, т.к. _process_approved_turn_locked глотает своё
    исключение и пишет его как in_progress/safe_reply)."""
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
    """Удалить тенант, ТОЛЬКО если он помечен _SENTINEL И без чужой активности. Возврат (полностью_чисто, why)."""
    from sqlalchemy import text
    from sreda.db.session import get_engine
    eng = get_engine()
    with eng.connect() as c:
        name = _tenant_name(c, tenant_id)

    tenant_present = name is not None
    if tenant_present and _SENTINEL not in (name or ""):
        return False, f"ОТКАЗ: тенант {tenant_id} без маркера (реальный?) — НЕ удаляю"
    if tenant_present and _has_foreign_activity(eng, tenant_id):
        return False, f"ОТКАЗ: у {tenant_id} есть реальная активность (чужой update_id) — НЕ удаляю"

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

    # signup_attempts (нет tenant_id) — по (channel, хеш источника). Чистим ВСЕГДА (rate-limit-строки
    # живут независимо от тенанта), с проверкой остатка (M4).
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

    # tenants последним — АТОМАРНО guardʼим маркером (если имя перестало быть маркированным между
    # проверкой и сюда — DELETE no-op, реальный тенант не удалим). Сбой НЕ глотаем (M2).
    blocker = ""
    try:
        with eng.begin() as c:
            c.execute(text("DELETE FROM tenants WHERE id = :t AND name LIKE :m"),
                      {"t": tenant_id, "m": f"{_SENTINEL}%"})
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
    from sreda.runtime.react_loop import _MAX_STEPS_MARKER
    from sqlalchemy import text

    eng = get_engine()
    # M3: сериализуем параллельные прогоны того же тира — держим advisory-lock на ОТДЕЛЬНОМ соединении
    # весь прогон. Второй прогон не возьмёт лок → ABORT (не тронет живой тенант первого). Только PG.
    lock_conn = None
    if _is_pg(eng):
        lock_conn = eng.connect()
        locked = lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                   {"k": _lock_key(tenant_id)}).scalar()
        if not locked:
            lock_conn.close()
            print(f"[smoke] ⛔ ABORT: параллельный прогон {tenant_id} уже идёт — не вмешиваюсь.")
            return 2

    # с этого момента любой выход обязан снять лок → общий try/finally (MINOR: утечка lock_conn)
    try:
        return await _run_smoke_locked(bot_key, chat, tenant_id, eng, TI, AA,
                                       get_session_factory, text, _MAX_STEPS_MARKER)
    finally:
        _release_lock(lock_conn, tenant_id)


async def _run_smoke_locked(bot_key, chat, tenant_id, eng, TI, AA,  # noqa: ANN001
                            get_session_factory, text, _MAX_STEPS_MARKER) -> int:
    fake = _FakeTelegramClient()
    TI.telegram_client_for = lambda *_a, **_k: fake  # type: ignore[assignment]
    # M1: нейтрализуем egress admin-алерта на всех слоях: функцию send_admin_alert (её импортируют в
    # момент вызова) и саму egress-границу _post_*_sync (belt против рефактора с ранней привязкой).
    _alerts: list = []
    AA.send_admin_alert = lambda *a, **k: _alerts.append((a, k))  # type: ignore[assignment]
    AA._post_telegram_sync = lambda *a, **k: True  # type: ignore[assignment]
    AA._post_max_sync = lambda *a, **k: True  # type: ignore[assignment]

    # СЛУЧАЙНЫЕ ОТРИЦАТЕЛЬНЫЕ update_id: наш namespace (слой 1) + разные прогоны не словят dedup даже
    # в одну секунду (Codex high MINOR: time-based совпал бы). 55 бит энтропии, всегда < 0.
    uid = -(1 + int.from_bytes(os.urandom(7), "big"))
    t0 = datetime.now(timezone.utc)  # watermark свежести: считаем только trace-строки этого прогона

    # fail-closed: если тенант queue-enabled, ход уйдёт отдельному воркеру, который НЕ видит наши
    # monkeypatch (fake-клиент, стаб алерта) → реальный egress + гонка с чисткой. Не гоним (Codex high).
    from sreda.workers.message_queue import is_queue_enabled_for
    if is_queue_enabled_for(tenant_id):
        print(f"[smoke] ⛔ ABORT: {tenant_id} в message-queue whitelist — воркер минует заглушки, не гоню.")
        return 2

    # verify-before-destroy: тенант с этим id уже есть?
    with get_session_factory()() as s:
        existing_name = _tenant_name(s, tenant_id)
    if existing_name is not None and _SENTINEL not in existing_name:
        print(f"[smoke] ⛔ ABORT: {tenant_id} существует БЕЗ маркера (реальный тенант?) — не трогаю.")
        return 2
    if existing_name is not None:  # наш остаток от прошлого прогона → чистим ДО старта
        ok, why = _cleanup_synthetic(tenant_id, chat)
        print(f"[smoke] пред-остаток (маркирован) очищен: {ok} ({why})")
        if not ok:  # brand-new гарантии больше нет → не гоним, чтобы старый trace не дал ложный PASS
            print(f"[smoke] ⛔ ABORT: пред-чистка не завершилась — прогон не brand-new.")
            return 2

    print(f"[smoke] bot_key={bot_key} tenant={tenant_id}")
    onboard_ok = True
    aborted = False
    try:
        await TI.handle_telegram_update(_update(uid, chat, "/start"), bot_key=bot_key)
        await _await_detached_tasks()
        with get_session_factory()() as s:
            created = s.execute(text("SELECT 1 FROM tenants WHERE id = :t"), {"t": tenant_id}).first()
            sub = s.execute(text("SELECT status FROM tenant_subscriptions WHERE tenant_id = :t"),
                            {"t": tenant_id}).first()
        sub_status = sub[0] if sub else None
        want_sub = _EXPECTED_SUB_STATUS.get(bot_key)
        sub_ok = (sub_status == want_sub)
        print(f"  [/start] тенант={bool(created)} подписка={sub_status} (ждём {want_sub}, ок={sub_ok})")
        onboard_ok = onboard_ok and bool(created) and sub_ok

        # Слой 1: перед тем как ПОМЕТИТЬ (штамп маркера = точка риска), докажем владение по update_id.
        if created and _has_foreign_activity(eng, tenant_id):
            print(f"[smoke] ⛔ ABORT: у {tenant_id} чужая активность (реальный update_id) — не помечаю, "
                  f"не удаляю. Разберись вручную.")
            aborted = True
            return 2
        if created:
            _mark_synthetic(tenant_id)  # безопасно: тенант доказанно наш

        n_before = len(fake.sent)
        excs = await _run_turn(TI, uid - 1, chat, bot_key)
        from sreda.db.models.react_trace import ReactTurnTrace
        with get_session_factory()() as s:
            rows = [r for r in s.query(ReactTurnTrace).filter(
                        ReactTurnTrace.tenant_id == tenant_id).all()
                    if r.created_at is not None and r.created_at >= t0]  # freshness
            n_ok = sum(1 for r in rows if r.status == "done" and (r.outcome or "") == "ok")
            n_stuck = sum(1 for r in rows if r.status == "in_progress")
            n_degraded = sum(1 for r in rows if (r.outcome or "") in _DEGRADED_OUTCOMES)
            # штопор: done/outcome=ok, но текст ответа = маркер max_steps (детект по тексту, не по outcome)
            n_maxsteps = sum(1 for r in rows if _MAX_STEPS_MARKER in (r.reply_text or ""))
        new_reply = len(fake.sent) - n_before
        if not rows:  # трейс пуст = persist выключен → не могу оценить здоровье, не greenlight'ю
            print(f"  [ход] ⚠️ трейс пуст (persist выключен?) — не могу оценить здоровье хода.")
            aborted = True
            return 2
        print(f"  [ход] трейс(свежие): ok={n_ok} in_progress(краш)={n_stuck} degraded={n_degraded} "
              f"штопор={n_maxsteps}; новых ответов={new_reply}; alert-подавлено={len(_alerts)}; "
              f"фон-исключений={len(excs)}")
        onboard_ok = (onboard_ok and n_ok >= 1 and n_stuck == 0 and n_degraded == 0
                      and n_maxsteps == 0 and new_reply >= 1 and not excs)
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"  [smoke] ❌ КРЭШ: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        onboard_ok = False
    finally:
        if aborted:  # владение не доказано → НЕ трогаем данные, оставляем оператору
            cleaned, why = False, "ABORT — чистку не делаю (владение не доказано)"
        else:
            try:
                cleaned, why = _cleanup_synthetic(tenant_id, chat)
            except Exception as e:  # noqa: BLE001 — сбой чистки = остаток → exit 2
                cleaned, why = False, f"чистка упала: {type(e).__name__}: {str(e)[:120]}"
        print(f"  [cleanup] тест-тенант удалён и чисто: {cleaned} ({why})")

    if aborted:
        return 2
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
