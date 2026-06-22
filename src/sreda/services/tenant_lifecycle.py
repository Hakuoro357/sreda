"""#187 soft-delete — рубильник жизненного цикла тенанта (Фаза 0: `is_tenant_active`).

Источник истины soft-delete: один флаг `tenants.deleted_at` + одна детерминированная
проверка `is_tenant_active`. Проверка вынесена ОТДЕЛЬНО, НЕ внутрь `EntitlementGate.check`
(тот зовётся из 8 мест, а `allowed` чтит лишь в 2 — голос/инструменты её пропустят; см.
plan `db-fix-tenant-deletion-plan.md`). Вызывается fail-closed во всех дверях отсечения.

Дальнейшие фазы добавят сюда `soft_delete_tenant` / `restore_tenant` (флаг + барьер + drain).
"""
from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, exists, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import Session

from sreda.db.models.core import OutboxMessage, Tenant
from sreda.db.models.inbound_event import InboundEvent
from sreda.db.models.message_jobs import MessageJob

logger = logging.getLogger(__name__)

# Единая причина-метка терминирования дренированных артефактов. Записывается в
# поля-причины (outbox.drop_reason / message_jobs.last_error /
# inbound_events.status_reason), которые читаются админкой/диагностикой.
_DRAIN_REASON = "tenant_deleted"

# Нетерминальные статусы inbound_events (см. db/models/inbound_event.py:79-89):
# new / needs_classification / classified — ещё «в работе»; consumed/skipped —
# уже терминальны и НЕ трогаются.
_INBOUND_NON_TERMINAL = ("new", "needs_classification", "classified")


def is_tenant_active(session: Session, tenant_id: str) -> bool:
    """True ⟺ тенант существует И НЕ помечен удалённым (`deleted_at IS NULL`).

    **Fail-closed:** нет строки тенанта → ``False`` (не исключение, не ``True``).
    Реализовано через ``EXISTS`` — не тащит строку, дёшево на горячем пути.
    """
    stmt = select(
        exists().where(Tenant.id == tenant_id, Tenant.deleted_at.is_(None))
    )
    return bool(session.execute(stmt).scalar())


# ---------------------------------------------------------------------------
# Барьер анти-гонки (Фаза 2b): session-scoped pg_advisory_lock на тенанта.
# ---------------------------------------------------------------------------


# Модульный кэш lock-engine по строковому URL. Один Engine на URL за процесс
# (lazy, NullPool) — БЕЗ churn'а Engine на каждый ход (R2 MAJOR). NullPool
# означает: соединения НЕ держатся в пуле; каждый ``engine.connect()`` открывает
# свежий физ.коннект, а ``conn.close()`` его реально рвёт (что отпускает
# session-scoped advisory-lock). Engine при этом переиспользуется (общий dialect/
# compiled-cache), сам соединений не удерживает. Thread-safe не требуется: под GIL
# гонка на dict в худшем случае создаст лишний Engine (безвредно — старый осиротеет
# и закроется GC); явный лок добавил бы сложности без выигрыша.
_LOCK_ENGINE_CACHE: dict[str, Engine] = {}


def _get_lock_engine(url) -> Engine:
    """Вернуть кэшированный (или создать) lock-engine для SQLAlchemy *url* (URL-объект).

    🔴 В ``create_engine`` передаётся URL-ОБЪЕКТ, НЕ ``str(url)``: у SQLAlchemy ``str(URL)``
    МАСКИРУЕТ пароль как ``***`` — на проде (``postgresql+psycopg://sreda:sreda@…``) коннект
    с паролем ``***`` упал бы, барьер не захватился, входящие ходы падали бы (Codex R3 CRITICAL).
    Ключ кэша — ``str(url)`` (маскированный годится как ключ: одинаковые сессии → один Engine).
    NullPool: см. комментарий к ``_LOCK_ENGINE_CACHE``.
    """
    key = str(url)
    engine = _LOCK_ENGINE_CACHE.get(key)
    if engine is None:
        engine = create_engine(url, poolclass=NullPool)
        _LOCK_ENGINE_CACHE[key] = engine
    return engine


def _tenant_advisory_lock_key(tenant_id: str) -> int:
    """Стабильный signed-64-bit ключ advisory-lock для *tenant_id*.

    Та же конвенция, что у ``workers/telegram_long_poll._advisory_lock_id``:
    SHA-256 от namespaced-строки → первые 8 байт → big-endian signed 64-bit.
    Детерминирован между перезапусками Python (в отличие от ``hash()``, который
    рандомизирован per-process через PYTHONHASHSEED), и попадает в диапазон,
    который принимает ``pg_advisory_lock`` (``bigint``).

    Namespace ``sreda-tenant-barrier:`` изолирует эти ключи от singleton-локов
    поллера (namespace ``sreda-telegram-poller:``) — коллизия между разными
    namespace при SHA-256 пренебрежимо мала.
    """
    digest = hashlib.sha256(
        f"sreda-tenant-barrier:{tenant_id}".encode()
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@contextmanager
def tenant_advisory_lock(session: Session, tenant_id: str) -> Iterator[None]:
    """Session-scoped advisory-lock на тенанта — БАРЬЕР анти-гонки (Фаза 2b).

    Контракт барьера: исполнение хода держит этот лок на ВЕСЬ ход (взять
    первым делом, отпустить в ``finally``); ``soft_delete_tenant`` берёт ТОТ ЖЕ
    лок ПЕРВЫМ (до чтения/выставления ``deleted_at`` и дренажа) → дренит ПОСЛЕ
    того как in-flight ход освободил лок. Так доменные мутации хода, прошедшего
    ingress-гейт, не коммитятся после зачистки.

    **Лок на ВЫДЕЛЕННОМ соединении, НЕ на рабочей сессии (R1 MAJOR).**
    Берём КЭШИРОВАННЫЙ engine (``NullPool``) на том же ``database_url``, что и
    рабочая сессия (URL берём из ``session.get_bind().engine``), открываем на нём
    отдельное соединение, берём ``pg_advisory_lock`` и держим до ``finally``.
    Зеркало ``workers/telegram_long_poll._lock_conn``: лок не зависит от состояния
    транзакции рабочей сессии (её rollback/commit не трогает наш lock-conn), и
    failed-рабочая-сессия не утаскивает lock-conn обратно в пул в грязном виде.

    **Engine кэшируется per-URL (R2 MAJOR), соединение — нет.** Раньше создавали
    НОВЫЙ ``create_engine`` на КАЖДЫЙ ход и диспоузили в ``finally`` — churn Engine
    на горячем пути. Теперь ``_get_lock_engine(url)`` отдаёт один Engine на URL за
    процесс; ``NullPool`` гарантирует, что выделенное соединение НЕ оседает в пуле,
    а ``conn.close()`` физически рвёт коннект (отпуская session-scoped lock).
    В ``finally``: ``pg_advisory_unlock`` → commit (закрыть autobegin, как R-23 в
    поллере) → **закрыть conn** (engine НЕ диспоузим — он переиспользуется).
    Закрытие соединения само по себе гарантированно отпускает session-scoped lock —
    даже если ``unlock`` бросил (``NullPool`` → close рвёт физ.коннект; никакого
    «idle in transaction»).

    **Session-scoped, НЕ xact.** ``pg_advisory_lock`` / ``pg_advisory_unlock``
    держатся до явного unlock или закрытия соединения — переживают mid-turn
    commit рабочей сессии. ``pg_advisory_xact_lock`` отпустился бы на первом
    промежуточном коммите хода (ход мульти-транзакционный) и барьер бы протёк.

    **SQLite (тесты): no-op** — advisory-локов нет; детект по
    ``session.get_bind().dialect.name``. На SQLite просто ``yield`` без SQL и без
    отдельного соединения: истинная сериализация — свойство PG-runtime, в
    SQLite-unit-тесте не воспроизводится (там проверяется только обвязка — что
    лок берётся первым и тем же ключом).

    Реентрантность: PG advisory-локи реентрантны в рамках ОДНОЙ сессии БД
    (одного backend-соединения). Здесь каждый барьер держит ОТДЕЛЬНОЕ
    соединение, поэтому два барьера на один ключ реально блокируются (это и
    нужно: ход и delete должны контендить). Один барьер берёт ключ ровно один
    раз → один unlock.
    """
    bind = session.get_bind()
    dialect = bind.dialect.name if bind is not None else ""
    if dialect != "postgresql":
        # SQLite / прочие без advisory-локов — корректный no-op (без отдельного
        # соединения: на SQLite advisory-локов нет, обвязка проверяется на ключе).
        yield
        return

    key = _tenant_advisory_lock_key(tenant_id)
    # КЭШИРОВАННЫЙ engine на URL рабочего engine (NullPool) — lock-conn полностью
    # изолирован от рабочего пула (зеркало _lock_conn в поллере), но Engine не
    # пересоздаётся на каждый ход (R2 MAJOR). Передаём URL-ОБЪЕКТ (НЕ str — иначе
    # пароль маскируется как ``***`` и коннект на PG падает, Codex R3 CRITICAL);
    # ключ кэша str() вычисляется ВНУТРИ _get_lock_engine.
    lock_engine = _get_lock_engine(bind.engine.url)
    # MINOR: если connect() упал — закрывать нечего (engine кэширован, не
    # диспоузим). Поэтому connect ВНЕ внутреннего try/finally: исключение из
    # connect() прокидывается без попытки close на None-соединении.
    lock_conn = lock_engine.connect()
    try:
        lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        # Закрыть autobegin-транзакцию: advisory-лок session-scoped, переживёт
        # commit; без commit соединение зависло бы в `idle in transaction`.
        lock_conn.commit()
        yield
    finally:
        # conn закрывается на ЛЮБОМ пути после успешного connect (вкл. провал
        # pg_advisory_lock выше): unlock (best-effort) → close. close физически
        # рвёт коннект (NullPool) и отпускает session-scoped lock даже если unlock
        # не прошёл. Engine НЕ диспоузим — он кэшируется/переиспользуется.
        try:
            lock_conn.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": key}
            )
            lock_conn.commit()
        except Exception:  # noqa: BLE001
            # unlock не прошёл — не страшно: close ниже физически рвёт соединение
            # (NullPool), что гарантированно отпускает session-scoped lock.
            # Логируем, не маскируем ход.
            logger.exception(
                "tenant_advisory_lock: pg_advisory_unlock failed for tenant %s "
                "(connection close will release the lock)",
                tenant_id,
            )
        finally:
            try:
                lock_conn.close()
            except Exception:  # noqa: BLE001
                pass


def soft_delete_tenant(session: Session, tenant_id: str) -> bool:
    """Помечает тенанта удалённым (`deleted_at`) И активно дренит pending-артефакты.

    **Управляет своей транзакцией — вызывающему commit НЕ нужен (R1 CRITICAL).**
    Флаг + drain коммитятся ВНУТРИ lock-scope (``session.commit()`` перед выходом
    из ``with``), поэтому лок держится до durable-commit. Иначе между unlock и
    commit вызывающего оставалось бы окно: in-flight ход уже отпустил лок, delete
    отпустил лок, но изменения ещё не durable — ход мог бы закоммитить доменную
    мутацию в это окно. Коммит под локом закрывает окно: к моменту unlock флаг
    уже durable.

    Возвращает ``True`` если тенант был помечен в этом вызове, ``False`` если
    он уже удалён (no-op — повторный drain не запускается).

    **Идемпотентность:** если ``tenant.deleted_at`` уже не NULL — НИЧЕГО не
    делаем (не пере-штампуем флаг, не дренируем повторно). Идемпотентный путь
    тоже коммитит под локом (чтобы лок гарантированно держался до durable-точки
    и на no-op-пути — барьер не должен зависеть от того, был ли реальный drain).

    **Drain (терминально, с причиной)** — точные поля/статусы сверены по моделям:
      - **outbox** (``status='pending'``) → ``status='dropped'`` +
        ``drop_reason='tenant_deleted'`` (валидное значение, читается в /stats).
      - **message_jobs** (``status IN ('pending','processing')``) → прямой UPDATE
        ``status='done', finished_at=now, last_error='tenant_deleted'``. НЕ
        ``mark_done`` (у него WHERE worker_id/attempt не матчит pending); прямой
        UPDATE проходит оба CHECK (enum + status/timestamps — ``done`` требует
        ``finished_at IS NOT NULL``).
      - **inbound_events** (new/needs_classification/classified) →
        ``status='skipped'`` + ``status_reason='tenant_deleted'``.
      - **family_reminders** — НЕ дренируем (ни one-shot, ни recurring; R1
        MAJOR анти-воскрешение). От доставки удалённого тенанта их защищает
        producer-фильтр ``due_now`` (JOIN tenants AND deleted_at IS NULL) +
        fencing-recheck воркера + Фаза 3 restore-drain. At-delete drain в
        ``fired`` создавал бы вектор воскрешения через ``snooze()`` (он
        безусловно возвращает ``status='pending'``).

    **Барьер (Фаза 2b):** всё тело (чтение флага + флаг + drain) обёрнуто в
    ``tenant_advisory_lock(session, tenant_id)`` — лок берётся ПЕРВЫМ делом, ДО
    чтения ``deleted_at``, и держится до конца (отпуск в ``finally`` помощника).
    Так удаление ждёт in-flight ход (тот держит ТОТ ЖЕ лок весь ход) и дренит
    только после него. На SQLite (тесты) лок — no-op (см. ``tenant_advisory_lock``).
    Воркеры вне барьера закрываются fencing-recheck в своих циклах (см.
    outbox_delivery / housewife_reminder_worker). Лок держится на ВЫДЕЛЕННОМ
    соединении (см. ``tenant_advisory_lock``) и отпускается ПОСЛЕ
    ``session.commit()`` рабочей сессии — durable-точка достигнута до unlock.
    """
    # Лок-FIRST: берём advisory-лок до любого чтения tenant-строки, чтобы
    # сериализоваться с in-flight ходом ещё до проверки ``deleted_at`` (иначе
    # между чтением флага и дренажем оставалось бы окно гонки). На идемпотентном
    # пути (уже удалён) лок тоже берётся — барьер не пропускается из-за того, что
    # строка «выглядит удалённой» на входе.
    with tenant_advisory_lock(session, tenant_id):
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            # Нет строки — нечего удалять; fail-closed-семантика на чтении это уже
            # трактует как «неактивен». Возвращаем False (не помечали). Коммитим
            # под локом (ничего не мутировали — это no-op-commit, но держит
            # инвариант «лок до durable-точки» единообразным на всех путях).
            logger.warning("soft_delete_tenant: tenant %s не найден — no-op", tenant_id)
            session.commit()
            return False

        if tenant.deleted_at is not None:
            # Уже удалён — идемпотентный no-op, drain не повторяем. Коммит под
            # локом (R1 CRITICAL): лок держится до durable-точки и на no-op-пути.
            session.commit()
            return False

        now = datetime.now(timezone.utc)
        tenant.deleted_at = now

        # --- outbox: pending → dropped ----------------------------------
        session.execute(
            update(OutboxMessage)
            .where(
                OutboxMessage.tenant_id == tenant_id,
                OutboxMessage.status == "pending",
            )
            .values(status="dropped", drop_reason=_DRAIN_REASON)
        )

        # --- message_jobs: pending/processing → done (прямой UPDATE) -----
        session.execute(
            update(MessageJob)
            .where(
                MessageJob.tenant_id == tenant_id,
                MessageJob.status.in_(("pending", "processing")),
            )
            .values(status="done", finished_at=now, last_error=_DRAIN_REASON)
        )

        # --- inbound_events: нетерминальные → skipped -------------------
        session.execute(
            update(InboundEvent)
            .where(
                InboundEvent.tenant_id == tenant_id,
                InboundEvent.status.in_(_INBOUND_NON_TERMINAL),
            )
            .values(status="skipped", status_reason=_DRAIN_REASON)
        )

        # --- family_reminders: НЕ дренируем (R1 MAJOR — анти-воскрешение) -
        # При удалении напоминания НЕ трогаем (ни one-shot, ни recurring). От
        # доставки удалённого тенанта их защищает producer-фильтр воркера
        # (``due_now`` JOIN tenants AND deleted_at IS NULL) + fencing-recheck +
        # Фаза 3 (restore-window-drain). At-delete drain был избыточен И создавал
        # вектор воскрешения: терминальный ``fired`` + ``next_trigger_at=NULL``
        # мог быть «оживлён» обратно в pending через ``snooze()`` (он безусловно
        # ставит ``status='pending'``). Чеклист A6 reminders НЕ требует — только
        # outbox / message_jobs / inbound_events.

        # R1 CRITICAL: коммитим флаг+drain ПОД локом (а не flush с commit'ом на
        # вызывающем) — лок держится до durable-commit, окно «unlock→commit»
        # закрыто. Управляем своей транзакцией; вызывающему commit НЕ нужен.
        session.commit()
        logger.info(
            "soft_delete_tenant: tenant %s помечен удалённым + drain выполнен",
            tenant_id,
        )
        return True
