"""#344 F5 — атомарный claim outbox-контура на РЕАЛЬНОМ Postgres (concurrency).

Аудит #336 F5. Unit-свит (SQLite) НЕ ловит гонку: нет ``FOR UPDATE SKIP
LOCKED`` и настоящей межтранзакционной конкуренции. Здесь — реальный PG16 +
ПОТОКИ (два воркера в отдельных соединениях), как red-suite #138 Ф3-d.

Claim НЕ вводит новых значений ``outbox_messages.status``: статус остаётся
'pending' во время lease, claim = поля ``claim_token``+``lease_expires_at``.
(F6 delivery_failed/durable-retry вынесен в #354 под F4-эпик — здесь только F5.)

Семантика: **at-least-once** (краш между send и commit → повторная доставка).
Exactly-once НЕ заявляем без идемпотентности мессенджера.

DSN через ``SREDA_PG_TEST_DSN`` (нет → skip; unit-прогоны не трогаются).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

pytestmark = pytest.mark.pg

_DSN = os.environ.get("SREDA_PG_TEST_DSN")
if not _DSN:
    pytest.skip(
        "SREDA_PG_TEST_DSN не задан — #344 concurrency-suite бежит только против реального Postgres",
        allow_module_level=True,
    )

# Safety guard (Codex sol CRITICAL): `pg_db` ниже делает ``DROP SCHEMA public
# CASCADE`` — если SREDA_PG_TEST_DSN случайно указывает на НЕ-throwaway БД, это
# уничтожит данные. Пускаем разрушение ТОЛЬКО против явно-тестовой локальной БД:
# scheme=postgresql, host в local-allowlist, имя БД строго test/test_*/*_test/
# *_test_* (никакого loose-substring вроде 'prod_testing'). Зеркалит
# test_message_queue_postgres_concurrency.py._safety_reason.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres", "pg", "db"})


def _pg_dsn_safety_reason(url: str) -> str | None:
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    if parsed.scheme not in {"postgresql", "postgres", "postgresql+psycopg"}:
        return f"scheme {parsed.scheme!r} is not postgresql"
    db_name = (parsed.path or "").lstrip("/").lower()
    if not db_name:
        return "DB name missing from DSN"
    if not (
        db_name == "test"
        or db_name.startswith("test_")
        or db_name.endswith("_test")
        or "_test_" in db_name
    ):
        return (
            f"DB name {db_name!r} doesn't look like a throwaway test DB "
            f"(must be 'test', 'test_*', '*_test', or '*_test_*')"
        )
    hosts = [(parsed.hostname or "").lower()]
    query = parse_qs(parsed.query or "")
    for param in ("host", "hostaddr"):
        for raw in query.get(param, []):
            hosts.extend(h.strip().lower() for h in raw.split(",") if h.strip())
    for host in hosts:
        if host and host not in _LOCAL_HOSTS:
            return (
                f"host {host!r} not in local-sandbox allowlist "
                f"{sorted(_LOCAL_HOSTS)} — refusing destructive DROP SCHEMA"
            )
    return None


_DSN_SAFETY = _pg_dsn_safety_reason(_DSN)
if _DSN_SAFETY is not None:
    pytest.skip(
        f"SREDA_PG_TEST_DSN не прошёл safety-гейт (DROP SCHEMA только на throwaway "
        f"тест-БД): {_DSN_SAFETY}",
        allow_module_level=True,
    )

TENANT = "tenant_ci_344"
WS = "ws_ci_344"
USER = "user_ci_344"


def _utc(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


@pytest.fixture()
def pg_db(monkeypatch):
    """Реальный PG-движок через шов; чистая схема + сид tenant/workspace/user."""
    monkeypatch.setenv("SREDA_DATABASE_URL", _DSN)
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY", "0" * 64)
    monkeypatch.setenv("SREDA_ENCRYPTION_KEY_ID", "primary")
    monkeypatch.setenv("SREDA_TG_ACCOUNT_SALT", "test-salt-344")
    from sreda.config.settings import get_settings
    get_settings.cache_clear()

    from sreda.db.base import Base
    import sreda.db.models  # noqa: F401 — регистрирует таблицы
    from sreda.db.session import get_engine, get_session_factory
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    eng = get_engine()
    # Чистый слепок схемы: throwaway-контейнер мог сохранить старую схему с
    # прошлых прогонов (create_all НЕ альтерит существующие таблицы). Пересоздаём
    # public с нуля — гарантирует актуальные колонки (claim_token/lease_expires_at).
    from sqlalchemy import text as _text
    with eng.begin() as c:
        c.execute(_text("DROP SCHEMA public CASCADE"))
        c.execute(_text("CREATE SCHEMA public"))
    Base.metadata.create_all(eng)

    from sreda.db.models.core import Tenant, User, Workspace
    S = get_session_factory()
    with S() as s:
        _clean(s)
        s.add(Tenant(id=TENANT, name="ci-344"))
        s.add(Workspace(id=WS, tenant_id=TENANT, name="Home"))
        s.add(User(id=USER, tenant_id=TENANT, telegram_account_id="424242"))
        s.commit()
    yield eng
    with S() as s:
        _clean(s)
        s.commit()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_settings.cache_clear()


def _clean(session) -> None:
    from sqlalchemy import text
    for tbl in ("outbox_messages", "inbound_messages", "family_reminders", "users",
                "workspaces", "tenants"):
        try:
            if tbl in ("users", "workspaces", "family_reminders", "outbox_messages",
                       "inbound_messages"):
                session.execute(text(f"DELETE FROM {tbl} WHERE tenant_id=:t"), {"t": TENANT})
            else:
                session.execute(text("DELETE FROM tenants WHERE id=:t"), {"t": TENANT})
        except Exception:
            session.rollback()


def _add_pending_outbox(session, *, rid: str | None = None, text: str = "привет") -> str:
    from sreda.db.models.core import OutboxMessage
    rid = rid or f"out_{uuid4().hex[:20]}"
    session.add(OutboxMessage(
        id=rid, tenant_id=TENANT, workspace_id=WS, user_id=USER,
        channel_type="telegram", status="pending", is_interactive=True,
        payload_json=json.dumps({"chat_id": "424242", "text": text, "reply_markup": None}),
        bot_key="sreda",
    ))
    session.commit()
    return rid


class CountingTelegram:
    """Thread-safe счётчик отправок + опциональная задержка для окна гонки."""

    def __init__(self, delay: float = 0.0) -> None:
        self._lock = threading.Lock()
        self.sent: list[str] = []
        self.delay = delay

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, **kw):
        if self.delay:
            await asyncio.sleep(self.delay)
        with self._lock:
            self.sent.append(str(chat_id))
        return {"ok": True, "result": {"message_id": len(self.sent)}}


def _run_worker(telegram, now: datetime, *, limit: int = 50) -> None:
    from sreda.workers.outbox_delivery import OutboxDeliveryWorker
    worker = OutboxDeliveryWorker(telegram_client=telegram)
    asyncio.run(worker.process_pending_messages(now=now, limit=limit))


def test_outbox_no_double_send_under_two_workers(pg_db):
    """Два конкурентных воркера (потоки, реальный PG) на ОДНУ pending-строку →
    РОВНО одна доставка. Claim эксклюзивен (lease). На коде БЕЗ claim оба
    выбирают строку и шлют дважды → тест краснеет."""
    from sreda.db.models.core import OutboxMessage
    from sreda.db.session import get_session_factory

    S = get_session_factory()
    with S() as s:
        rid = _add_pending_outbox(s)

    now = _utc(2026, 5, 1, 12, 0)
    tg_a = CountingTelegram(delay=0.4)
    tg_b = CountingTelegram(delay=0.4)
    barrier = threading.Barrier(2)

    def run(tg):
        barrier.wait()
        _run_worker(tg, now)

    threads = [threading.Thread(target=run, args=(tg_a,)), threading.Thread(target=run, args=(tg_b,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    total_sends = len(tg_a.sent) + len(tg_b.sent)
    # Семантика контракта — at-least-once; claim даёт ЭКСКЛЮЗИВНОСТЬ конкурентного
    # захвата (не exactly-once): при одновременном claim строку берёт РОВНО один воркер.
    assert total_sends == 1, f"concurrent claim exclusivity violated: {total_sends} отправок"

    with S() as s:
        row = s.get(OutboxMessage, rid)
        assert row.status == "sent"


def test_reclaim_fence_skips_row_reclaimed_by_other_worker_before_send(pg_db):
    """R2 MAJOR — атомарный per-row fence. Моделируем переклейм МЕЖДУ claim'ом
    воркера A и его отправкой: строку заклеймил A (token A), затем другой воркер
    переклеймил её (сменил claim_token). `_reclaim_row(token_A)` обязан вернуть
    False → A НЕ отправляет (доставит новый владелец) → нет двойной доставки."""
    from sreda.db.models.core import OutboxMessage
    from sreda.db.session import get_session_factory, tenant_session
    from sreda.workers.outbox_delivery import OutboxDeliveryWorker

    now = _utc(2026, 5, 1, 12, 0)
    S = get_session_factory()
    with S() as s:
        rid = _add_pending_outbox(s)
        # Воркер A заклеймил (token A, lease в будущем).
        row = s.get(OutboxMessage, rid)
        row.claim_token = "worker_A"
        row.lease_expires_at = now + timedelta(seconds=60)
        s.commit()

    worker = OutboxDeliveryWorker()

    # Пока A "собирался слать", строку переклеймил воркер B.
    with S() as s:
        row = s.get(OutboxMessage, rid)
        row.claim_token = "worker_B"
        row.lease_expires_at = now + timedelta(seconds=60)
        s.commit()

    # Fence воркера A: владение утрачено → False (A не шлёт).
    with tenant_session(TENANT) as s:
        owns_a = worker._reclaim_row(s, rid, "worker_A")
    assert owns_a is False, "переклейменную строку A слать НЕ должен"

    # Fence воркера B: владеет → True + lease продлён.
    with tenant_session(TENANT) as s:
        owns_b = worker._reclaim_row(s, rid, "worker_B")
    assert owns_b is True, "текущий владелец B доставляет"


def test_send_timeout_bounds_delivery_and_keeps_pending_for_retry(pg_db, monkeypatch):
    """R3 MAJOR — жёсткий таймаут отправки. Если send длится дольше лимита (>
    per-row lease был бы возможен) — он ПРЕРЫВАЕТСЯ, строка остаётся pending,
    claim снят → повтор на следующем тике. Гарантирует send < lease, значит
    другой воркер не переклеймит во время нашей отправки. Моделируем медленный
    send + маленький таймаут."""
    import sreda.workers.outbox_delivery as odm
    from sreda.db.models.core import OutboxMessage
    from sreda.db.session import get_session_factory

    monkeypatch.setattr(odm, "OUTBOX_SEND_TIMEOUT_SECONDS", 0.3)

    class SlowTelegram:
        def __init__(self):
            self.started = 0

        async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, **kw):
            self.started += 1
            await asyncio.sleep(2.0)  # дольше таймаута 0.3с
            return {"ok": True, "result": {"message_id": 1}}

    S = get_session_factory()
    with S() as s:
        rid = _add_pending_outbox(s)

    tg = SlowTelegram()
    _run_worker(tg, _utc(2026, 5, 1, 12, 0), limit=1)

    assert tg.started == 1, "отправка была начата (и прервана таймаутом)"
    with S() as s:
        row = s.get(OutboxMessage, rid)
        assert row.status == "pending", "таймаут доставки → строка остаётся pending для повтора"
        assert row.claim_token is None, "claim снят → следующий тик подхватит сразу"


def test_per_row_deadline_bounds_whole_delivery_and_releases_claim(pg_db, monkeypatch):
    """R4 MAJOR — ЕДИНЫЙ дедлайн на ВСЮ доставку строки (не только на один send).
    Моделируем медленную последовательность (send не превышает свой таймаут, но
    общий путь дольше per-row дедлайна). Дедлайн < lease → строка остаётся pending,
    claim снят, дубля нет. Проверяет, что суммарная последовательность ограничена."""
    import sreda.workers.outbox_delivery as odm
    from sreda.db.models.core import OutboxMessage
    from sreda.db.session import get_session_factory

    # Дедлайн строки мал; send-таймаут больше дедлайна → сработает именно ДЕДЛАЙН
    # всей доставки, а не таймаут одного send.
    monkeypatch.setattr(odm, "OUTBOX_ROW_DEADLINE_SECONDS", 0.4)
    monkeypatch.setattr(odm, "OUTBOX_SEND_TIMEOUT_SECONDS", 5.0)

    class SlowSeqTelegram:
        def __init__(self):
            self.started = 0

        async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, **kw):
            self.started += 1
            await asyncio.sleep(2.0)  # < send-таймаут 5с, но > дедлайна строки 0.4с
            return {"ok": True, "result": {"message_id": 1}}

    S = get_session_factory()
    with S() as s:
        rid = _add_pending_outbox(s)

    tg = SlowSeqTelegram()
    _run_worker(tg, _utc(2026, 5, 1, 12, 0), limit=1)

    with S() as s:
        row = s.get(OutboxMessage, rid)
        assert row.status == "pending", "превышение дедлайна доставки → строка остаётся pending"
        assert row.claim_token is None, "claim снят после дедлайна → повтор без ожидания lease"


def test_late_claim_release_does_not_wipe_another_workers_claim(pg_db):
    """R5 MAJOR — запоздалый release после timeout НЕ стирает чужой claim.
    Сценарий: воркер A превысил дедлайн; пока его отмена завершалась, lease истёк
    и строку переклеймил воркер B (claim_token=B). Запоздалый
    `_release_claim_by_id(token=A)` воркера A НЕ должен трогать claim B (иначе
    третий воркер заберёт строку параллельно с B → двойная доставка)."""
    from sreda.db.models.core import OutboxMessage
    from sreda.db.session import get_session_factory
    from sreda.workers.outbox_delivery import OutboxDeliveryWorker

    now = _utc(2026, 5, 1, 12, 0)
    S = get_session_factory()
    with S() as s:
        rid = _add_pending_outbox(s)
        # Воркер B уже переклеймил строку (после истечения lease A).
        row = s.get(OutboxMessage, rid)
        row.claim_token = "worker_B"
        row.lease_expires_at = now + timedelta(seconds=60)
        s.commit()

    # Запоздалый release воркера A (его токен — worker_A).
    OutboxDeliveryWorker()._release_claim_by_id(TENANT, rid, "worker_A")

    with S() as s:
        row = s.get(OutboxMessage, rid)
        assert row.claim_token == "worker_B", "чужой (B) claim не должен быть стёрт запоздалым release A"
        assert row.lease_expires_at is not None, "lease B сохранён"


class _SimulatedCrash(BaseException):
    """BaseException — НЕ ловится ``except Exception`` в воркере, моделирует
    жёсткий краш процесса (SIGKILL) после send, до commit статуса."""


class CrashOnceTelegram:
    """Первый send: записывает и роняет процесс (BaseException) ДО commit'а
    статуса → строка остаётся pending+claimed. Последующие — обычная доставка."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._crashed = False

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, **kw):
        self.sent.append(str(chat_id))
        if not self._crashed:
            self._crashed = True
            raise _SimulatedCrash("process died after send, before commit")
        return {"ok": True, "result": {"message_id": len(self.sent)}}


def test_outbox_crash_after_send_before_commit_is_at_least_once_not_silent_loss(pg_db):
    """Краш между HTTP-send и commit → строка НЕ теряется (остаётся
    pending+claimed), по истечении lease переклеймливается и доставляется снова.
    Итог: at-least-once (2 фактических send'а), финально 'sent' — НЕ тихая потеря.
    (Exactly-once НЕ заявляем без идемпотентности мессенджера.)"""
    from sreda.db.models.core import OutboxMessage
    from sreda.db.session import get_session_factory

    S = get_session_factory()
    with S() as s:
        rid = _add_pending_outbox(s)

    tg = CrashOnceTelegram()
    t0 = _utc(2026, 5, 1, 12, 0)
    # Тик 1: claim + send (записан) + краш ДО commit статуса. limit=1 → lease=минимум.
    try:
        _run_worker(tg, t0, limit=1)
    except _SimulatedCrash:
        pass  # процесс «умер»

    with S() as s:
        row = s.get(OutboxMessage, rid)
        assert row.status == "pending", "после краша строка НЕ теряется — остаётся pending"
        assert row.claim_token is not None, "claim держится (после краша)"
        # Симулируем ИСТЕЧЕНИЕ lease (время прошло) — ставим срок в прошлое, чтобы
        # следующий тик мог переклеймить (per-row fence продлевает lease от _now(),
        # поэтому детерминированно «состариваем» строку прямой правкой поля).
        row.lease_expires_at = _utc(2020, 1, 1)
        s.commit()
    assert len(tg.sent) == 1

    # Тик 2: lease истёк → переклейм → повторная доставка (at-least-once).
    _run_worker(tg, t0 + timedelta(seconds=1), limit=1)

    with S() as s:
        row = s.get(OutboxMessage, rid)
        assert row.status == "sent"
    assert len(tg.sent) == 2, "at-least-once: доставлено дважды (краш + повтор)"


def test_crash_mid_batch_tail_recovers_fast_short_batch_lease(pg_db):
    """Субагент-ревью past-R5 MAJOR — batch-lease НЕ масштабируется размером батча.
    Краш мид-батч оставляет НЕДОСТАВЛЕННЫЙ ХВОСТ с claim'ом; его lease обязан быть
    КОРОТКИМ (batch-lease-константа), иначе рестартнутый воркер пропустил бы хвост
    на ~13 мин (регрессия recovery). Проверяем: (1) lease хвоста ≤ короткой
    константы; (2) рестарт чуть позже неё доставляет хвост.

    RED на старом коде (lease = max(60, limit×per-row) = 2000с при limit=50):
    assert `lease <= batch_const+1` красный."""
    from datetime import timezone as _tz
    from sreda.db.models.core import OutboxMessage
    from sreda.db.session import get_session_factory
    from sreda.workers.outbox_delivery import OUTBOX_BATCH_LEASE_SECONDS

    now = _utc(2026, 5, 1, 12, 0)
    S = get_session_factory()
    ids = []
    with S() as s:
        for i in range(3):
            rid = f"out_tail_{i}"
            s.add(OutboxMessage(
                id=rid, tenant_id=TENANT, workspace_id=WS, user_id=USER,
                channel_type="telegram", status="pending", is_interactive=True,
                payload_json=json.dumps({"chat_id": "424242", "text": f"m{i}", "reply_markup": None}),
                bot_key="sreda", created_at=now - timedelta(seconds=10 - i),  # ascending
            ))
            ids.append(rid)
        s.commit()

    class CrashOnFirst:
        def __init__(self):
            self.sent = []

        async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, **kw):
            self.sent.append(text)
            raise _SimulatedCrash("crash on first send, mid-batch")

    tg = CrashOnFirst()
    try:
        _run_worker(tg, now)  # limit=50 → клеймит все 3 одним батчем
    except _SimulatedCrash:
        pass  # процесс «умер» на первой отправке
    assert len(tg.sent) == 1  # краш на ПЕРВОЙ доставке → 2 строки не достигнуты

    def _as_utc(dt):
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=_tz.utc)

    # Порядок claim/RETURNING произволен → не знаем ЗАРАНЕЕ, какие 2 строки остались
    # нетронутыми. Но их РОВНО 2, и у них КОРОТКИЙ batch-lease. На старом коде (lease
    # масштабирован limit'ом = ~2000с) таких «коротких» было бы 0 → RED.
    short_lease_pending = 0
    with S() as s:
        for rid in ids:
            row = s.get(OutboxMessage, rid)
            if row.status == "pending" and _as_utc(row.lease_expires_at) <= now + timedelta(
                seconds=OUTBOX_BATCH_LEASE_SECONDS + 1
            ):
                short_lease_pending += 1
    assert short_lease_pending == 2, (
        f"ожидали 2 недостигнутых хвоста с КОРОТКИМ batch-lease, получили {short_lease_pending} "
        f"(батч-lease масштабирован размером батча → рестарт застрял бы на ~13 мин)"
    )

    # Рестартнутый воркер ЧУТЬ ПОСЛЕ короткого batch-lease → доставляет хвост быстро
    # (а не через ~13 мин).
    tg2 = CountingTelegram()
    _run_worker(tg2, now + timedelta(seconds=OUTBOX_BATCH_LEASE_SECONDS + 1))
    with S() as s:
        delivered = sum(1 for rid in ids if s.get(OutboxMessage, rid).status == "sent")
    assert delivered >= 2, (
        "недостигнутый хвост передоставлен вскоре после короткого batch-lease "
        f"(delivered={delivered})"
    )


def test_worker_without_claim_logic_still_delivers_pending_with_lease_fields_set(pg_db):
    """Rollback-инвариант (R2-D, §4): claim НЕ вводит новых ЗНАЧЕНИЙ status —
    строка остаётся 'pending' во время lease. Воркер СТАРОЙ версии (игнорит
    claim_token/lease_expires_at, выбирает по status='pending') всё равно
    доставляет заклеймленную строку → откат новой версии не застревает.

    Моделируем: строка pending, заклеймлена ЧУЖИМ токеном с lease в БУДУЩЕМ
    (как будто её держит новый воркер). Легаси-доставка (status-only SELECT,
    без учёта claim) обязана её доставить."""
    from sreda.db.models.core import OutboxMessage
    from sreda.db.session import get_session_factory, privileged_session, tenant_session

    now = _utc(2026, 5, 1, 12, 0)
    S = get_session_factory()
    with S() as s:
        rid = _add_pending_outbox(s)
        row = s.get(OutboxMessage, rid)
        row.claim_token = "new_worker_token"
        row.lease_expires_at = now + timedelta(seconds=300)  # lease в будущем
        s.commit()

    tg = CountingTelegram()

    async def legacy_deliver():
        """Эмуляция pre-#344 воркера: SELECT по status='pending' БЕЗ учёта
        claim-полей → send → status='sent'."""
        from sqlalchemy import or_ as _or
        with privileged_session("queue-dispatch") as scan:
            ids = [
                (r.id, r.tenant_id)
                for r in scan.query(OutboxMessage).filter(
                    OutboxMessage.status == "pending",
                    OutboxMessage.channel_type.in_(("telegram", "max")),
                    _or(OutboxMessage.scheduled_at.is_(None),
                        OutboxMessage.scheduled_at <= now),
                ).order_by(OutboxMessage.created_at.asc()).all()
            ]
        for row_id, tid in ids:
            with tenant_session(tid) as s2:
                r = s2.get(OutboxMessage, row_id)
                if r is None or r.status != "pending":
                    continue
                payload = json.loads(r.payload_json)
                await tg.send_message(payload["chat_id"], payload.get("text", ""))
                r.status = "sent"
                s2.commit()

    asyncio.run(legacy_deliver())

    assert len(tg.sent) == 1, "легаси-воркер (без claim-логики) обязан доставить pending-строку"
    with S() as s:
        assert s.get(OutboxMessage, rid).status == "sent"


class FailThenSucceedTelegram:
    """Первый send → TelegramDeliveryError; далее — успех. Для retry-теста."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, **kw):
        self.attempts += 1
        if self.attempts == 1:
            from sreda.integrations.telegram.client import TelegramDeliveryError
            raise TelegramDeliveryError("transient")
        return {"ok": True, "result": {"message_id": self.attempts}}


def test_delivery_retry_eventually_delivers(pg_db):
    """Retry-контур: первый тик — TelegramDeliveryError → строка остаётся
    pending, claim СНЯТ (быстрый повтор). Второй тик — успех → 'sent'. Ответ
    не потерян."""
    from sreda.db.models.core import OutboxMessage
    from sreda.db.session import get_session_factory

    S = get_session_factory()
    with S() as s:
        rid = _add_pending_outbox(s)

    tg = FailThenSucceedTelegram()
    t0 = _utc(2026, 5, 1, 12, 0)
    _run_worker(tg, t0)
    with S() as s:
        row = s.get(OutboxMessage, rid)
        assert row.status == "pending"
        assert row.claim_token is None, "после ошибки доставки claim снят → быстрый повтор"

    _run_worker(tg, t0 + timedelta(seconds=1))
    with S() as s:
        assert s.get(OutboxMessage, rid).status == "sent"
    assert tg.attempts == 2


def test_reminder_delivery_dedup_holds_under_two_workers(pg_db):
    """R2-F регрессия: два конкурентных reminder-воркера (потоки) на ОДНО due
    напоминание → РОВНО одна outbox-строка (idempotency_key + partial-unique
    backstop). Fencing #344 сюда не мешает — оба видят due-pending; дедуп держит
    детерминированный advance + ключ по fired_trigger."""
    from datetime import UTC
    from sreda.db.models.core import OutboxMessage
    from sreda.db.session import get_session_factory
    from sreda.services.housewife_reminders import HousewifeReminderService
    from sreda.workers.housewife_reminder_worker import HousewifeReminderWorker

    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    S = get_session_factory()
    with S() as s:
        svc = HousewifeReminderService(s)
        svc.schedule(
            tenant_id=TENANT, user_id=USER,
            title="Двойной тик", trigger_at=now - timedelta(minutes=1),
        )
        s.commit()

    barrier = threading.Barrier(2)

    def run():
        barrier.wait()
        asyncio.run(HousewifeReminderWorker().process_pending(now=now))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    with S() as s:
        n = s.query(OutboxMessage).filter(OutboxMessage.tenant_id == TENANT).count()
    assert n == 1, f"reminder delivery-dedup нарушен: {n} outbox-строк (ожидалась 1)"


class _FakeTelegramForCallback:
    """Мини-заглушка TelegramClient для реального ``_handle_reminder_callback``:
    сетевые вызовы идут ПОСЛЕ ``session.commit()`` — важно, что лок к тому моменту
    уже отпущен, поэтому здесь просто фиксируем факт вызова."""

    def __init__(self) -> None:
        self.answered: list[str] = []
        self.edited: list[str] = []

    async def answer_callback_query(self, callback_id, text=None, **kw):
        self.answered.append(str(text))
        return {"ok": True}

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None, **kw):
        self.edited.append(str(text))
        return {"ok": True}


def _snooze_callback_query(rid: str) -> dict:
    return {
        "id": "cb_344",
        "message": {
            "chat": {"id": "424242"},
            "message_id": 55,
            "text": "🔔 Полить цветы",
        },
    }


def test_reminder_worker_does_not_clobber_concurrent_snooze(pg_db, monkeypatch):
    """#344 F5 (Opus-адверсар + Codex sol, MAJOR cross-process ack/snooze гонка).

    Кнопки «Сделал ✅»/«Отложить ⏰» обрабатываются в UVICORN-процессе
    (``telegram_bot._handle_reminder_callback``), а reminder-fire-цикл крутится в
    ОТДЕЛЬНОМ JOB_RUNNER — второй конкурентный писатель в ``family_reminders`` уже
    при ОДНОМ воркере. Обе стороны читали строку plain ``s.get`` (без ``FOR
    UPDATE``). Гонка НЕ закрывается локом ТОЛЬКО у воркера: ``snooze`` — это
    read-modify-write, а SQLAlchemy кладёт в UPDATE лишь реально изменившиеся
    поля. ``snooze`` не меняет ``status`` (``pending``→``pending`` не dirty).
    Поэтому даже если воркер под локом закоммитит ``fired``, слепой (без re-read)
    UPDATE обработчика поставит только ``next_trigger_at=+10мин`` и оставит
    рассинхрон ``status='fired'`` + ``next_trigger_at`` в будущем → snooze тихо
    потерян (напоминание не вернётся: ``due_now`` фильтрует ``status='pending'``).

    Фикс (обе стороны берут ``SELECT ... FOR UPDATE`` и держат лок до commit):
    воркер и обработчик СЕРИАЛИЗУЮТСЯ на строке. Обработчик, дождавшись воркера,
    перечитывает СВЕЖЕЕ состояние (``fired``) → ``fired→pending`` становится
    настоящим изменением и попадает в UPDATE → финально ``status='pending'`` с
    отложенным ``next_trigger_at``.

    Тест гоняет РЕАЛЬНЫЙ ``_handle_reminder_callback`` (покрывает callback-side
    лок) конкурентно с ``process_pending``. Snooze инъектируется из
    ``_enqueue_outbox_for`` (между fence и ``mark_fired``) в отдельном
    потоке/соединении. Детерминизм БЕЗ sleep (Codex terra): ждём, пока snooze либо
    закоммитится (baseline — лока нет), либо ЗАБЛОКИРУЕТСЯ на локе строки
    (``pg_locks.NOT granted`` — fixed), и лишь тогда пускаем воркер в ``mark_fired``.

    RED на baseline (ни воркер, ни обработчик не лочат): snooze коммитится в окне
    fence→mark_fired → ``mark_fired`` перетирает → финально ``status='fired'``,
    ``next_trigger_at=None``."""
    from datetime import UTC

    from sqlalchemy import text as _sql_text

    import sreda.workers.housewife_reminder_worker as _hrw
    from sreda.db.models.housewife import FamilyReminder
    from sreda.db.session import get_session_factory
    from sreda.services.housewife_reminders import HousewifeReminderService
    from sreda.services.telegram_bot import _handle_reminder_callback
    from sreda.workers.housewife_reminder_worker import HousewifeReminderWorker

    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    S = get_session_factory()
    with S() as s:
        rem = HousewifeReminderService(s).schedule(
            tenant_id=TENANT, user_id=USER,
            title="Полить цветы", trigger_at=now - timedelta(minutes=1),
        )
        s.commit()
        rid = rem.id

    snooze_done = threading.Event()
    snooze_error: list[BaseException] = []
    fake_tg = _FakeTelegramForCallback()
    real_enqueue = _hrw.HousewifeReminderWorker._enqueue_outbox_for

    def _someone_waiting_on_row_lock() -> bool:
        # Изолированная тест-БД, одна строка → незагранённый лок = snooze ждёт
        # наш FOR UPDATE (детерминированная замена sleep, Codex terra).
        with S() as probe:
            return bool(
                probe.execute(
                    _sql_text("SELECT count(*) FROM pg_locks WHERE NOT granted")
                ).scalar()
            )

    def enqueue_with_concurrent_snooze(self, session, reminder):
        # Реальный обработчик кнопки «Отложить» из uvicorn-процесса (своя сессия/
        # соединение, свой event loop). Отдельный поток — иначе его блокирующийся
        # под FOR UPDATE reread заклинил бы сам воркер.
        def run_callback():
            try:
                with S() as cs:
                    asyncio.run(
                        _handle_reminder_callback(
                            session=cs,
                            telegram_client=fake_tg,
                            callback_query=_snooze_callback_query(rid),
                            data=f"rem_snooze:{rid}",
                        )
                    )
                snooze_done.set()
            except BaseException as exc:  # noqa: BLE001
                snooze_error.append(exc)
                snooze_done.set()

        t = threading.Thread(target=run_callback, daemon=True)
        t.start()
        # Ждём, пока snooze закоммитится (baseline) ИЛИ заблокируется на нашем локе
        # (fixed) — только тогда двигаемся к mark_fired. Без фиксированного sleep.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if snooze_done.is_set() or _someone_waiting_on_row_lock():
                break
            time.sleep(0.05)
        else:
            raise AssertionError(
                "snooze за 20с ни закоммитился, ни заблокировался на локе — "
                "тест не смог смоделировать гонку"
            )
        return real_enqueue(self, session, reminder)

    monkeypatch.setattr(
        _hrw.HousewifeReminderWorker, "_enqueue_outbox_for",
        enqueue_with_concurrent_snooze,
    )

    asyncio.run(HousewifeReminderWorker().process_pending(now=now))
    # На фикс-коде snooze добивается только после commit тика (release лока).
    assert snooze_done.wait(timeout=20), "snooze-поток не завершился за 20с (возможен deadlock)"
    assert not snooze_error, f"snooze-поток упал: {snooze_error!r}"

    with S() as s:
        row = s.get(FamilyReminder, rid)
        assert row.status == "pending", (
            "snooze юзера ПОТЕРЯН: остался status='fired' — воркер/обработчик слепо "
            "перетёрли snooze (нужен FOR UPDATE re-read с ОБЕИХ сторон)"
        )
        # Обработчик зовёт snooze без now-override → next_trigger = реальное now+10мин
        # (не инъектированный now воркера). Достаточно проверить, что он отложен в
        # будущее (snooze применён), а не обнулён mark_fired'ом.
        assert row.next_trigger_at is not None, "snooze должен был отложить next_trigger_at"
        _ntt = row.next_trigger_at
        if _ntt.tzinfo is None:
            _ntt = _ntt.replace(tzinfo=timezone.utc)
        assert _ntt > datetime.now(timezone.utc) + timedelta(minutes=9), (
            "next_trigger_at должен быть отложен ~на 10мин в будущее (snooze), "
            f"получили {_ntt!r}"
        )
        # Ровно одна outbox-строка — воркер фаернул один раз, дубля/повторного пинга нет.
        n_out = s.query(OutboxMessage).filter(OutboxMessage.tenant_id == TENANT).count()
        assert n_out == 1, f"ожидалась ровно 1 outbox-строка (фаер воркера), получили {n_out}"
