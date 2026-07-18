"""#187 soft-delete — рубильник жизненного цикла тенанта (Фаза 0: `is_tenant_active`).

Источник истины soft-delete: один флаг `tenants.deleted_at` + одна детерминированная
проверка `is_tenant_active`. Проверка вынесена ОТДЕЛЬНО, НЕ внутрь `EntitlementGate.check`
(тот зовётся из 8 мест, а `allowed` чтит лишь в 2 — голос/инструменты её пропустят; см.
plan `db-fix-tenant-deletion-plan.md`). Вызывается fail-closed во всех дверях отсечения.

Дальнейшие фазы добавят сюда `soft_delete_tenant` / `restore_tenant` (флаг + барьер + drain).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, exists, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import Session

from sreda.db.models.core import OutboxMessage, Tenant
from sreda.db.models.housewife import FamilyReminder
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

    lock_conn = _acquire_lock_conn(bind, tenant_id)
    try:
        yield
    finally:
        _release_lock_conn(lock_conn, tenant_id)


def _acquire_lock_conn(bind, tenant_id: str):
    """Блокирующий захват lock-conn + ``pg_advisory_lock`` (обвязка барьера).

    🔴 В async-контексте вызывать ТОЛЬКО через ``asyncio.to_thread``
    (audit-fix 2026-07-18, svc-security MINOR #6): ``pg_advisory_lock`` —
    синхронный блокирующий вызов psycopg; выполненный в потоке event loop,
    он останавливает ВЕСЬ loop (все tenants/HTTP) на время ожидания лока.
    """
    key = _tenant_advisory_lock_key(tenant_id)
    # КЭШИРОВАННЫЙ engine на URL рабочего engine (NullPool) — lock-conn полностью
    # изолирован от рабочего пула (зеркало _lock_conn в поллере), но Engine не
    # пересоздаётся на каждый ход (R2 MAJOR). Передаём URL-ОБЪЕКТ (НЕ str — иначе
    # пароль маскируется как ``***`` и коннект на PG падает, Codex R3 CRITICAL);
    # ключ кэша str() вычисляется ВНУТРИ _get_lock_engine.
    lock_engine = _get_lock_engine(bind.engine.url)
    # MINOR: если connect() упал — закрывать нечего (engine кэширован, не
    # диспоузим). Поэтому connect ВНЕ внутреннего try: исключение из
    # connect() прокидывается без попытки close на None-соединении.
    lock_conn = lock_engine.connect()
    try:
        lock_conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        # Закрыть autobegin-транзакцию: advisory-лок session-scoped, переживёт
        # commit; без commit соединение зависло бы в `idle in transaction`.
        lock_conn.commit()
    except Exception:
        # Захват/коммит не удался — закрываем conn сами (close рвёт
        # физ.коннект → даже частично взятый лок гарантированно отпущен),
        # исключение прокидываем вызывающему.
        try:
            lock_conn.close()
        except Exception:  # noqa: BLE001
            pass
        raise
    return lock_conn


def _release_lock_conn(lock_conn, tenant_id: str) -> None:
    """Best-effort ``pg_advisory_unlock`` → close (зеркало finally sync-версии).

    Блокирующий вызов — в async-контексте только через ``asyncio.to_thread``.
    """
    key = _tenant_advisory_lock_key(tenant_id)
    # unlock (best-effort) → close. close физически рвёт коннект (NullPool) и
    # отпускает session-scoped lock даже если unlock не прошёл. Engine НЕ
    # диспоузим — он кэшируется/переиспользуется.
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


@asynccontextmanager
async def tenant_advisory_lock_async(
    session: Session, tenant_id: str
) -> AsyncIterator[None]:
    """Async-вариант ``tenant_advisory_lock`` (audit-fix 2026-07-18,
    svc-security MINOR #6) — для входа из корутины.

    Захват и освобождение ``pg_advisory_lock`` — блокирующие psycopg-вызовы;
    sync-версия, вошедшая из async-хода (``ExitStack.enter_context`` в
    корутине), останавливала ВЕСЬ event loop на время ожидания лока
    (все tenants/HTTP), если ``soft_delete_tenant`` держит тот же ключ.
    Здесь оба блокирующих шага уносятся в ``asyncio.to_thread`` — loop
    продолжает обслуживать остальные корутины, пока этот ход ждёт барьер.

    Семантика (session-scoped lock на отдельном NullPool-conn, кэшированный
    engine, SQLite no-op) — та же, что у sync-версии; см. её docstring.
    """
    bind = session.get_bind()
    dialect = bind.dialect.name if bind is not None else ""
    if dialect != "postgresql":
        yield
        return

    lock_conn = await asyncio.to_thread(_acquire_lock_conn, bind, tenant_id)
    try:
        yield
    finally:
        await asyncio.to_thread(_release_lock_conn, lock_conn, tenant_id)


def _write_lifecycle_audit(
    session: Session,
    *,
    action: str,
    tenant_id: str,
    actor_type: str | None,
    actor_id: str | None,
    source: str | None,
    reason: str | None,
    extra_metadata: dict | None = None,
):
    """Записать audit_log-строку для lifecycle-действия ПОД текущей транзакцией.

    Вызывается ВНУТРИ ``soft_delete_tenant`` / ``restore_tenant`` (под advisory-
    локом, ДО внутреннего ``session.commit()``), поэтому audit-строка коммитится
    атомарно с флагом+drain'ом: durable-флаг ⟺ есть его audit-запись. ``commit=
    False`` — транзакцией управляет вызывающая lifecycle-функция; здесь только
    ``session.add``+``flush``. Self-delete (Фаза 4b-2) переиспользует ЭТОТ путь,
    передав ``actor_type='user'`` + свой ``action`` через те же параметры.

    Аудит пишется ТОЛЬКО если задан ``action`` И ``actor_type`` (admin/self
    передают оба). Для внутренних/системных вызовов без actor (если появятся)
    — параметры опускаются → audit не пишется (поведение Фаз 0-4a не меняется).

    **Возврат (R1 MAJOR — строгий аудит).** Когда actor задан — возвращаем
    ``AuditLog``-строку или ``None``, если ``audit_event`` НЕ записал её
    (невалидный ``actor_type`` → audit.py:71-75, либо flush-ошибка + rollback →
    audit.py:94-103). Вызывающий ОБЯЗАН проверить: ``None`` при заданном actor
    означает, что аудит-строки нет (или сессия уже откачена) — флаг коммитить
    НЕЛЬЗЯ (иначе нарушится A11 «committed-флаг ⟺ audit-строка»). Когда actor
    НЕ задан (системный путь Фаз 0-4a) — возвращаем ``None`` БЕЗ записи; для
    этого пути ``None`` ожидаем и не является ошибкой.
    """
    if not action or not actor_type:
        return None
    from sreda.services.audit import audit_event

    metadata: dict = {}
    if source is not None:
        metadata["source"] = source
    if reason is not None:
        metadata["reason"] = reason
    if extra_metadata:
        metadata.update(extra_metadata)
    return audit_event(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        resource_type="tenant",
        resource_id=tenant_id,
        metadata=metadata,
        commit=False,
    )


def soft_delete_tenant(
    session: Session,
    tenant_id: str,
    *,
    actor_type: str | None = None,
    actor_id: str | None = None,
    source: str | None = None,
    reason: str | None = None,
    audit_action: str = "admin.tenant.soft_delete",
) -> bool:
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

    **Аудит (A11):** если переданы ``actor_type`` (admin/self) — пишем ОДНУ
    ``audit_log``-строку ``audit_action`` (по умолчанию ``admin.tenant.soft_delete``)
    ПОД локом, ДО внутреннего commit'а → атомарно с флагом+drain'ом (committed-
    флаг ⟺ его audit-запись). На идемпотентном/нет-строки no-op-пути audit НЕ
    пишется (не было реального действия → нет второй строки на повторе). Без
    ``actor_type`` (системный вызов) — audit опускается (поведение Фаз 0-4a).

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

        # A11 аудит: пишем audit-строку ПОД локом, ДО commit'а — атомарно с
        # флагом+drain'ом (committed-флаг ⟺ его audit-запись). Только на пути
        # реального удаления (idempotent/no-row no-op выходят выше → нет второй
        # строки на повторе). metadata.source='admin'|'self' + reason +
        # previous_deleted_at=None (был активен — нижняя точка для трассировки).
        audit_row = _write_lifecycle_audit(
            session,
            action=audit_action,
            tenant_id=tenant_id,
            actor_type=actor_type,
            actor_id=actor_id,
            source=source,
            reason=reason,
            extra_metadata={"previous_deleted_at": None},
        )
        # R1 MAJOR (строгий аудит): когда actor задан (admin/self), аудит-строка
        # ОБЯЗАНА быть записана. ``audit_event`` best-effort возвращает None при
        # невалидном actor_type ИЛИ при flush-ошибке (тогда сессия УЖЕ откачена)
        # — в обоих случаях commit ниже выставил бы флаг БЕЗ audit-строки (или
        # вернул бы ``True`` на откаченной мутации). Поднимаем ДО commit'а: флаг
        # не коммитится, вызывающий (админ-эндпойнт) получает ошибку, не ложный
        # ``ok``. Лок отпустится в ``finally`` ``tenant_advisory_lock`` (raise
        # выходит из ``with``-блока), рабочая транзакция откатится вызывающим/
        # контекст-менеджером сессии. Системный путь (actor_type=None) сюда не
        # доходит: _write_lifecycle_audit вернул None БЕЗ записи, но и проверка
        # пропущена (actor_type is None) → коммит как прежде (Фазы 0-4a).
        if actor_type is not None and audit_row is None:
            # Codex R2 MAJOR: при невалидном actor_type audit_event возвращает None
            # БЕЗ rollback → pending флаг+drain остаются в сессии. Откатываем САМИ
            # перед raise (не полагаясь на вызывающего): иначе caller, поймавший
            # RuntimeError, мог бы потом commit()'нуть флаг без аудит-строки.
            session.rollback()
            raise RuntimeError(
                f"lifecycle audit write failed for tenant {tenant_id} "
                f"(action={audit_action}) — refusing to commit flag without audit"
            )

        # R1 CRITICAL: коммитим флаг+drain ПОД локом (а не flush с commit'ом на
        # вызывающем) — лок держится до durable-commit, окно «unlock→commit»
        # закрыто. Управляем своей транзакцией; вызывающему commit НЕ нужен.
        session.commit()
        logger.info(
            "soft_delete_tenant: tenant %s помечен удалённым + drain выполнен",
            tenant_id,
        )
        return True


def restore_tenant(
    session: Session,
    tenant_id: str,
    *,
    actor_type: str | None = None,
    actor_id: str | None = None,
    source: str | None = None,
    reason: str | None = None,
    audit_action: str = "admin.tenant.restore",
) -> bool:
    """Снимает `deleted_at` И зачищает окно удаления `[deleted_at, restored_at]`.

    Симметрично ``soft_delete_tenant``: работает ПОД тем же session-scoped
    advisory-lock и коммитит ПОД локом (Фаза 3). Возвращает ``True`` если тенант
    был восстановлен в этом вызове, ``False`` если он уже активен (no-op).

    **Идемпотентность:** если ``tenant.deleted_at IS NULL`` (уже активен или
    никогда не удалялся) — НИЧЕГО не делаем (return False). Нет строки → тоже
    False (нечего восстанавливать).

    **Аудит (A11):** симметрично ``soft_delete_tenant`` — если передан
    ``actor_type``, пишем ОДНУ ``audit_log``-строку ``audit_action`` (по умолчанию
    ``admin.tenant.restore``) ПОД локом, ДО commit'а (атомарно с обнулением флага +
    зачисткой окна). metadata несёт ``previous_deleted_at`` (значение до restore —
    для трассировки длительности удаления). На no-op-пути audit НЕ пишется.

    **Зачистка окна удаления (анти-воскрешение, A2).** За время удаления
    producer-фильтр воркеров не давал тенанту ничего слать, но pending-артефакты
    с триггером ВНУТРИ окна остались «взведёнными». Если просто снять флаг — они
    выстрелят пачкой сразу после restore (просрочка). Окно — ИМЕННО
    ``[deleted_at, restored_at]`` (обе границы включительно): дренируем то, чей
    триггер/появление попали В ЭТОТ ИНТЕРВАЛ. Артефакты, просроченные ЕЩЁ ДО
    удаления (триггер ``< deleted_at``), НЕ трогаем — они не результат удаления
    (R1 MAJOR: без нижней границы был over-reach). ``deleted_at`` читаем ДО
    обнуления флага:

      - **one-shot reminders** (``recurrence_rule IS NULL``, ``status='pending'``,
        ``deleted_at <= next_trigger_at <= restored_at``) → ``status='cancelled'`` +
        ``next_trigger_at=None`` (терминал, как штатный ``cancel()``). Они
        просрочены за период удаления → пользователю уже не актуальны. Берём
        ``cancelled``, НЕ ``fired``: ``snooze()`` безусловно ставит
        ``status='pending'`` и мог бы воскресить ``fired`` — ``cancelled``
        безопаснее (а эти и не доставлялись).
      - **recurring reminders** (``recurrence_rule`` НЕ NULL, ``status='pending'``,
        ``deleted_at <= next_trigger_at <= restored_at``) → сдвиг ``next_trigger_at``
        на следующее вхождение СТРОГО ПОСЛЕ ``restored_at`` (пропущенные за период
        удаления НЕ слать). Через общий ``compute_next_occurrence_after`` (тот же,
        что в ``mark_fired``/``acknowledge``). Если будущих вхождений нет
        (исчерпан ``COUNT``/``UNTIL``) → ``status='fired'`` + ``next_trigger_at=
        None`` (как в воркере).
      - **inbound_events** нетерминальные (new/needs_classification/classified),
        у которых ``created_at`` В ``[deleted_at, restored_at]`` ИЛИ ``classified_at``
        В ``[deleted_at, restored_at]`` → ``status='skipped'`` +
        ``status_reason='tenant_deleted'`` (защитно; симметрично one-shot — иначе
        ``list_ready_for_delivery`` оживил бы старый проактив после restore).
        ``classified_at`` nullable (inbound_event.py:95) → «new» (без classified_at)
        ловится парой по ``created_at``; NULL-сравнение по classified_at тихо не
        матчит, предикат не ломается.

    Дренированное (``cancelled``/``fired``/``skipped``/advanced) НЕ воскрешается:
    статус терминальный или триггер уже в будущем относительно ``restored_at``.

    Будущие напоминания (триггер ``> restored_at``, вне окна) и просроченные ДО
    удаления (``< deleted_at``) НЕ трогаются — первые взведены легитимно (сработают
    позже), вторые не относятся к удалению.
    """
    from sreda.services.housewife_reminders import compute_next_occurrence_after

    with tenant_advisory_lock(session, tenant_id):
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            # Нет строки — нечего восстанавливать. Коммит под локом (инвариант
            # «лок до durable-точки» единообразен на всех путях, как в delete).
            logger.warning("restore_tenant: tenant %s не найден — no-op", tenant_id)
            session.commit()
            return False

        if tenant.deleted_at is None:
            # Уже активен (или не удалялся) — идемпотентный no-op. Коммит под
            # локом (симметрично delete-no-op-пути).
            session.commit()
            return False

        # 🔴 Нижняя граница окна — СОХРАНИТЬ deleted_at ДО обнуления флага.
        # Окно зачистки — ИМЕННО ``[deleted_at, restored_at]`` (включительно с
        # обоих концов): артефакт, просроченный/созданный ВНУТРИ периода удаления,
        # дренируем; артефакт, просроченный ЕЩЁ ДО удаления (next_trigger_at <
        # deleted_at), НЕ трогаем — он не результат удаления, у него своя судьба
        # (его проворонил/обработал воркер до удаления). Без нижней границы был
        # over-reach: restore отменял/сдвигал напоминания, к удалению отношения
        # не имевшие (R1 MAJOR).
        # Нормализуем deleted_at к aware-UTC: на PG ``DateTime(timezone=True)``
        # отдаёт aware, на SQLite (тесты) — naive. Без нормализации Python-side
        # evaluator bulk-UPDATE (``synchronize_session='evaluate'``) падал бы
        # ``can't compare offset-naive and offset-aware`` на сравнении naive
        # deleted_at с aware created_at/next_trigger_at. Делаем обе границы окна
        # aware — сравнения единообразны на любом диалекте.
        deleted_at = tenant.deleted_at
        if deleted_at is not None and deleted_at.tzinfo is None:
            deleted_at = deleted_at.replace(tzinfo=timezone.utc)
        restored_at = datetime.now(timezone.utc)
        tenant.deleted_at = None

        # --- one-shot reminders в окне → cancelled (терминал) ------------
        # ``synchronize_session=False``: ДБ выполняет WHERE (включая datetime-
        # сравнения), без Python-side evaluator, который мешал бы naive/aware
        # datetime'ы на смешанных строках. Вызывающий перечитывает строки
        # (``refresh``) после commit — устаревшего in-session-стейта нет.
        session.execute(
            update(FamilyReminder)
            .where(
                FamilyReminder.tenant_id == tenant_id,
                FamilyReminder.status == "pending",
                FamilyReminder.recurrence_rule.is_(None),
                FamilyReminder.next_trigger_at.isnot(None),
                FamilyReminder.next_trigger_at >= deleted_at,
                FamilyReminder.next_trigger_at <= restored_at,
            )
            .values(status="cancelled", next_trigger_at=None, updated_at=restored_at),
            execution_options={"synchronize_session": False},
        )

        # --- recurring reminders в окне → сдвиг за restored_at ------------
        # Построчно (rrule per-row): берём pending recurring, чей next_trigger_at
        # внутри окна [deleted_at, restored_at], и двигаем на следующее вхождение
        # СТРОГО ПОСЛЕ restored_at.
        due_recurring = (
            session.query(FamilyReminder)
            .filter(
                FamilyReminder.tenant_id == tenant_id,
                FamilyReminder.status == "pending",
                FamilyReminder.recurrence_rule.isnot(None),
                FamilyReminder.next_trigger_at.isnot(None),
                FamilyReminder.next_trigger_at >= deleted_at,
                FamilyReminder.next_trigger_at <= restored_at,
            )
            .all()
        )
        for reminder in due_recurring:
            next_occ = compute_next_occurrence_after(
                reminder.recurrence_rule, reminder.trigger_at, restored_at
            )
            if next_occ is None:
                # Вхождений после restored_at нет (исчерпан COUNT/UNTIL) →
                # терминал, как в воркере при пустом next_occ.
                reminder.status = "fired"
                reminder.next_trigger_at = None
            else:
                reminder.next_trigger_at = next_occ
            reminder.updated_at = restored_at

        # --- inbound_events нетерминальные В ОКНЕ → skipped --------------
        # Окно — ``[deleted_at, restored_at]`` (нижняя граница включительно).
        # Цель: событие, СОЗДАННОЕ в окне (``created_at``) ИЛИ ставшее
        # классифицированным в окне (``classified_at``). Две независимые пары
        # диапазонов, каждая со своими границами:
        #   - created_at  В [deleted_at, restored_at]  — ловит «new» (у которых
        #     ``classified_at IS NULL``: предикат по classified_at на NULL даёт
        #     NULL/неопределённость, поэтому «new» ловится ТОЛЬКО этой парой);
        #   - classified_at В [deleted_at, restored_at] — ловит события,
        #     классифицированные в окне (даже если созданы раньше удаления —
        #     переход в нетерминальный «classified» случился внутри окна).
        # ``classified_at`` nullable (db/models/inbound_event.py:95) → на NULL
        # вторая пара тихо не матчит (NULL-сравнение = не-TRUE), предикат не
        # ломается. Без нижней границы был over-reach: skip'ались и события,
        # созданные/классифицированные ЕЩЁ ДО удаления (R1 MAJOR).
        from sqlalchemy import and_, or_

        session.execute(
            update(InboundEvent)
            .where(
                InboundEvent.tenant_id == tenant_id,
                InboundEvent.status.in_(_INBOUND_NON_TERMINAL),
                or_(
                    and_(
                        InboundEvent.created_at >= deleted_at,
                        InboundEvent.created_at <= restored_at,
                    ),
                    and_(
                        InboundEvent.classified_at >= deleted_at,
                        InboundEvent.classified_at <= restored_at,
                    ),
                ),
            )
            .values(status="skipped", status_reason=_DRAIN_REASON),
            # См. one-shot выше: ДБ-side WHERE, без Python evaluator (naive/aware).
            execution_options={"synchronize_session": False},
        )

        # A11 аудит: ПОД локом, ДО commit'а — атомарно с обнулением флага +
        # зачисткой окна. previous_deleted_at = значение ДО restore (iso-строка) —
        # для трассировки, сколько тенант был удалён. Только на пути реального
        # restore (no-op'ы вышли выше → нет лишней строки на повторе).
        audit_row = _write_lifecycle_audit(
            session,
            action=audit_action,
            tenant_id=tenant_id,
            actor_type=actor_type,
            actor_id=actor_id,
            source=source,
            reason=reason,
            extra_metadata={"previous_deleted_at": deleted_at.isoformat()},
        )
        # R1 MAJOR (строгий аудит, симметрично soft_delete): actor задан → аудит-
        # строка ОБЯЗАНА быть. None при заданном actor = не записалась (невалидный
        # actor_type) ИЛИ flush-ошибка с уже откаченной сессией → raise ДО commit'а,
        # чтобы не снять флаг + не зачистить окно БЕЗ audit-строки и не вернуть
        # ложный ``ok``. Лок отпустится в ``finally`` барьера, транзакция откатится
        # вызывающим. Системный путь (actor_type=None) проверку пропускает.
        if actor_type is not None and audit_row is None:
            # Codex R2 MAJOR (симметрично soft_delete): откатываем pending снятие
            # флага + зачистку окна САМИ перед raise — невалидный actor_type не
            # делает rollback в audit_event, оставляя транзакцию грязной.
            session.rollback()
            raise RuntimeError(
                f"lifecycle audit write failed for tenant {tenant_id} "
                f"(action={audit_action}) — refusing to commit restore without audit"
            )

        # Коммит ПОД локом (симметрично soft_delete): флаг+drain durable до unlock.
        session.commit()
        logger.info(
            "restore_tenant: tenant %s восстановлен + окно удаления зачищено",
            tenant_id,
        )
        return True
