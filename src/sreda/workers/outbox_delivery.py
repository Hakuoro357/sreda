"""Outbox delivery worker (Phase 2d).

Polls the ``outbox_messages`` queue and routes each pending row through
the per-user delivery policy:

  * ``send``  → Telegram send + status='sent'
  * ``defer`` → set ``scheduled_at`` to end-of-quiet-window, leave pending
  * ``drop``  → status='muted' (user set ``priority=mute`` for this skill)

Runs in the same polling loop as the skill-platform processor. The
cadence is defined by ``Settings.job_poll_interval_seconds``.

Note: interactive replies (replies to user commands) are already sent
inline by ``node_persist_replies`` — they arrive at the worker with
``status='sent'`` or ``'pending'`` (delivery retry). The worker just
retries those pending rows.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import or_, text, update
from sqlalchemy.orm import Session

from sreda.config.bot_registry import (
    LEGACY_NULL_BOT_KEY,
    TelegramBotRegistry,
    telegram_client_for,
)
from sreda.db.models.core import OutboxMessage
from sreda.db.repositories.user_profile import UserProfileRepository
from sreda.db.session import privileged_session, tenant_session
from sreda.features.app_registry import get_feature_registry
from sreda.integrations.max.client import MaxClient, MaxDeliveryError
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.runtime.delivery_policy import DeliveryKind, decide_delivery
from sreda.services import trace

logger = logging.getLogger(__name__)

# #344 F5 — параметры атомарного claim'а с lease.
#
# Начальный claim БАТЧА берёт КОРОТКИЙ КОНСТАНТНЫЙ lease `OUTBOX_BATCH_LEASE_SECONDS`
# (НЕ масштабируем размером батча — субагент-ревью past-R5 MAJOR: lease = limit×per-row
# при прод limit=20 давал ~13 мин; краш/рестарт мид-батч оставлял недоставленный ХВОСТ
# с длинным lease → рестартнутый процесс считал его не-claimable ~13 мин → регрессия
# recovery даже на ОДНОМ процессе). Короткий batch-lease → recovery хвоста за ~минуту.
#
# ГЛАВНАЯ защита от двойной доставки — НЕ длина batch-lease, а АТОМАРНЫЙ per-row fence
# `_reclaim_row` НЕПОСРЕДСТВЕННО перед отправкой: продлевает lease от СВЕЖЕГО `_now()`
# (не от начала тика — R2/R3 MAJOR) на `OUTBOX_LEASE_PER_ROW_SECONDS` и проверяет
# владение по claim_token. Если хвост переклеймил другой/рестартнутый воркер (сменил
# claim_token), наш `_reclaim_row(старый_token)` фейлит `WHERE claim_token=старый` →
# строку пропускаем → нет двойной доставки. Вся доставка строки под жёстким дедлайном
# `OUTBOX_ROW_DEADLINE_SECONDS` (< per-row lease) + пер-send таймаут
# `OUTBOX_SEND_TIMEOUT_SECONDS`: пока шлём, lease валиден, переклейма нет. Остаток =
# краш после send до commit = документированный at-least-once.
OUTBOX_BATCH_LEASE_SECONDS = 60
OUTBOX_LEASE_PER_ROW_SECONDS = 40
# Жёсткий дедлайн на ВСЮ доставку ОДНОЙ строки (весь исходящий путь: для MAX это
# до 2× edit + fallback send + cleanup, для TG — send). Строго МЕНЬШЕ per-row lease
# (R4 Codex high+medium MAJOR: отдельные send-таймауты не ограничивали СУММУ MAX
# edit→fallback→cleanup, которая могла превысить lease → переклейм во время
# доставки). Дедлайн < lease гарантирует, что пока идёт доставка, наш lease ещё
# валиден и другой воркер строку не переклеймит. Превышение → строка остаётся
# pending, claim снимается → повтор (at-least-once).
OUTBOX_ROW_DEADLINE_SECONDS = 25
# Жёсткий таймаут ОДНОЙ сетевой отправки (defense-in-depth внутри общего дедлайна).
OUTBOX_SEND_TIMEOUT_SECONDS = 20

# Аудит 2026-07-18 (#2) — счётчик попыток + потолок ретраев + dead-letter.
# Раньше ЛЮБАЯ ошибка доставки (включая перманентные 4xx «bot was blocked
# by the user» / «chat not found») оставляла строку 'pending' → failing-
# вызов к TG/MAX API каждый тик бессрочно, poison-строки копились (retention
# чистит только sent/failed/dropped), а метрика reliability_report (считает
# только 'failed') врала «всё ок».
#
# Перманентный отказ (HTTP 4xx, кроме 429 rate-limit и кроме 401/403 —
# R1 M15: системные auth-ошибки канала, retryable) → dead-letter СРАЗУ:
# status='failed' + drop_reason='delivery_permanent_<code>'. Транзиентные
# (401/403 / 429 / 5xx / сеть) → ретрай с потолком OUTBOX_MAX_DELIVERY_ATTEMPTS
# попыток, затем dead-letter: status='failed' +
# drop_reason='delivery_retry_exhausted' + admin alert. 'failed' виден
# reliability_report → метрика честная; retention чистит failed через 60д.
#
# Потолок 500 при тике 5–10с ≈ 40–80 минут транзиентного сбоя до dead-
# letter: кратковременный инцидент канала строки не убивает, настоящий
# poison всё же финализируется.
#
# ОГРАНИЧЕНИЕ: счётчик in-memory (модульный) — сбрасывается рестартом
# процесса (poison-строка получит ещё N попыток). Durable-варианту нужна
# колонка outbox_messages.attempts + миграция (вне scope этого фикса —
# см. финальный отчёт воркера).
OUTBOX_MAX_DELIVERY_ATTEMPTS = 500
_DELIVERY_ATTEMPTS: dict[str, int] = {}


class OutboxDeliveryWorker:
    def __init__(
        self,
        telegram_client: TelegramClient | None = None,
        max_client: "MaxClient | None" = None,
        registry: TelegramBotRegistry | None = None,
    ) -> None:
        # #138 Ф2: воркер сам ведёт скоупы (privileged-скан очереди + tenant_session
        # на строку); общую сессию больше не принимает.
        self.telegram = telegram_client
        # MAX channel client (Phase 6 of MAX integration sprint).
        # None — MAX channel delivery будет skip'аться (fallback chain
        # попробует TG если у юзера есть и telegram_account_id).
        self.max = max_client
        # Phase 4a: bot registry for multi-bot routing.  When set,
        # _send_now resolves the client via telegram_client_for(row.bot_key).
        # None falls back to self.telegram (backward-compat / tests that
        # don't wire a registry).
        self.registry = registry

    async def process_pending_messages(
        self, *, now: datetime | None = None, limit: int = 50
    ) -> int:
        now_utc = now or datetime.now(timezone.utc)
        # #138 Ф2: очередь доставки outbox — КРОСС-ТЕНАНТНАЯ (все семьи) →
        # privileged. #344 F5: АТОМАРНЫЙ claim — не голый SELECT (два воркера
        # выбирали ту же pending-строку и слали дважды). Клеймим батч эксклюзивно
        # (claim_token + lease_expires_at) и КОММИТИМ claim ДО доставки, чтобы
        # второй воркер не увидел claimed-строки. status остаётся 'pending'.
        with privileged_session("queue-dispatch") as scan:
            row_ids, token = self._claim_batch(scan, now_utc=now_utc, limit=limit)
        processed = 0
        for row_id, tenant_id in row_ids:
            # Пер-строчная изоляция: сбой одной доставки не роняет тик (в отличие
            # от старой общей сессии — но send/статус-переходы и так ловили ошибки внутри).
            try:
                with tenant_session(tenant_id) as s:
                    # #344 F5 (R2/R3 MAJOR — оба ревьюера): АТОМАРНЫЙ per-row fence
                    # НЕПОСРЕДСТВЕННО перед доставкой. Одним conditional UPDATE
                    # (WHERE claim_token=наш AND status='pending') подтверждаем
                    # владение по СВЕЖЕМУ состоянию БД и продлеваем lease от СВЕЖЕГО
                    # времени (`_now()`, не now_utc начала тика). 0 строк → нас
                    # переклеймили / строка не pending → пропуск (доставит владелец).
                    if not self._reclaim_row(s, row_id, token):
                        continue
                    row = s.get(OutboxMessage, row_id)
                    if row is None or row.status != "pending":
                        continue  # исчезла / уже обработана между claim и доставкой
                    # #344 F5 (R4 MAJOR): ВЕСЬ исходящий путь строки под ЕДИНЫМ
                    # жёстким дедлайном < lease. Так суммарная MAX-последовательность
                    # (edit×2 + fallback send + cleanup) не превысит lease → пока
                    # доставляем, наш lease валиден, переклейма нет.
                    await asyncio.wait_for(
                        self._process_one(s, row, now_utc=now_utc),
                        timeout=OUTBOX_ROW_DEADLINE_SECONDS,
                    )
                processed += 1
            except asyncio.TimeoutError:
                # Дедлайн доставки превышен. Дедлайн < lease → мы ВСЁ ЕЩЁ владелец
                # (никто не переклеймил), поэтому безопасно снять claim в свежей
                # сессии → повтор на следующем тике сразу (at-least-once).
                logger.warning(
                    "outbox delivery: row %s exceeded per-row deadline %ss — "
                    "release claim, retry next tick", row_id, OUTBOX_ROW_DEADLINE_SECONDS,
                )
                self._release_claim_by_id(tenant_id, row_id, token)
                continue
            except Exception:  # noqa: BLE001
                logger.exception("outbox delivery: failed for row %s", row_id)
                continue
        return processed

    def _release_claim_by_id(self, tenant_id: str, row_id: str, token: str) -> None:
        """#344 F5 (R4/R5) — снять claim со строки в СВЕЖЕЙ сессии (после дедлайна
        доставки исходная сессия откачена/непригодна). Только пока строка pending И
        claim_token ВСЁ ЕЩЁ наш (R5 MAJOR: `asyncio.wait_for` ждёт завершения отмены,
        поэтому фактический возврат может быть позже дедлайна; если к этому моменту
        lease истёк и строку переклеймил другой воркер, наш запоздалый release НЕ
        должен стирать ЧУЖОЙ активный claim — иначе третий воркер заберёт строку
        параллельно с новым владельцем). WHERE claim_token=наш → чужой claim не трогаем.
        Best-effort."""
        try:
            with tenant_session(tenant_id) as s:
                res = s.execute(
                    update(OutboxMessage)
                    .where(
                        OutboxMessage.id == row_id,
                        OutboxMessage.status == "pending",
                        OutboxMessage.claim_token == token,
                    )
                    .values(claim_token=None, lease_expires_at=None)
                )
                s.commit()
                if res.rowcount == 0:
                    logger.info(
                        "outbox delivery: claim on %s уже не наш (переклеймлен) — "
                        "release-noop", row_id,
                    )
        except Exception:  # noqa: BLE001
            logger.warning(
                "outbox delivery: failed to release claim on %s after deadline",
                row_id, exc_info=True,
            )

    @staticmethod
    async def _send_bounded(coro, exc_cls):
        """#344 F5 (R3 MAJOR) — жёсткий таймаут на ОДНУ отправку: send не может
        длиться дольше per-row lease. Таймаут → трактуем как сбой доставки
        (пробрасываем DeliveryError канала → строка остаётся pending, claim
        снимается, повтор на следующем тике; at-least-once). Без этого медленная
        отправка (>lease) позволила бы другому воркеру переклеймить и отправить
        дубль в НОРМАЛЬНОЙ работе."""
        try:
            return await asyncio.wait_for(coro, timeout=OUTBOX_SEND_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise exc_cls("outbox send exceeded hard timeout") from exc

    @staticmethod
    def _now() -> datetime:
        """Текущее время (UTC). Отдельный метод — чтобы per-row fence продлевал
        lease от СВЕЖЕГО времени, а не от снимка начала тика, и чтобы тесты могли
        детерминированно моделировать истечение lease без реального ожидания."""
        return datetime.now(timezone.utc)

    def _reclaim_row(self, session: Session, row_id: str, token: str) -> bool:
        """#344 F5 (R2/R3 MAJOR) — атомарно подтвердить владение claim'ом и продлить
        lease на одну отправку. Возвращает True, если строка всё ещё наша
        (claim_token совпал и она pending) — тогда lease продлён и можно слать;
        False, если её переклеймил другой воркер или она уже терминальна.

        Fencing DB-authoritative: conditional UPDATE атомарен, владение — по
        claim_token. Lease продлевается от СВЕЖЕГО `_now()` (не от начала тика — R3
        MAJOR: у поздней строки батча снимок начала тика дал бы уже истёкший lease).
        В паре с жёстким таймаутом отправки (< per-row lease) это гарантирует, что
        пока мы шлём строку, её lease валиден и другой воркер её не переклеймит →
        нет steady-state двойной доставки. Остаточное окно = краш = at-least-once.
        """
        new_lease = self._now() + timedelta(seconds=OUTBOX_LEASE_PER_ROW_SECONDS)
        res = session.execute(
            update(OutboxMessage)
            .where(
                OutboxMessage.id == row_id,
                OutboxMessage.claim_token == token,
                OutboxMessage.status == "pending",
            )
            .values(lease_expires_at=new_lease)
        )
        session.commit()
        return res.rowcount == 1

    def _claim_batch(
        self, scan: Session, *, now_utc: datetime, limit: int
    ) -> tuple[list[tuple[str, str]], str]:
        """#344 F5 — атомарно заклеймить до ``limit`` доставляемых строк.

        Клеймим строки ``status='pending'`` доставляемых каналов, срок которых
        наступил и которые НЕ заклеймлены (или lease истёк). Claim выражается
        полями ``claim_token``/``lease_expires_at`` — значение ``status`` НЕ
        меняется (rollback-safety, §4). COMMIT внутри scan-транзакции фиксирует
        claim ДО доставки, поэтому второй воркер не выберет ту же строку.

        Lease батча — КОРОТКИЙ КОНСТАНТНЫЙ (`OUTBOX_BATCH_LEASE_SECONDS`), НЕ
        масштабируется размером батча (past-R5 MAJOR: длинный lease страндил
        недоставленный хвост при краше на ~13 мин). Владение доставкой держит per-row
        `_reclaim_row` (продлевает lease перед отправкой). Возвращает (список
        (id, tenant_id), claim_token).

        Фильтр claimable: `claim_token IS NULL OR lease_expires_at IS NULL OR
        lease_expires_at < now` — ветка `lease IS NULL` защищает от латентного
        стрэндинга (token set + lease NULL никогда бы не прошёл `NULL<now`; MINOR).

        PostgreSQL — ``FOR UPDATE SKIP LOCKED`` в под-SELECT (лок В ШАГЕ claim,
        не в скане; два воркера получают непересекающиеся наборы). SQLite (unit) —
        без SKIP LOCKED (одно соединение), true-concurrency покрыта PG-тестами.
        """
        token = uuid4().hex
        lease_until = now_utc + timedelta(seconds=OUTBOX_BATCH_LEASE_SECONDS)
        bind = scan.bind
        if bind is not None and bind.dialect.name == "postgresql":
            sql = text(
                """
                UPDATE outbox_messages
                   SET claim_token = :tok, lease_expires_at = :lease
                 WHERE id IN (
                     SELECT id FROM outbox_messages
                      WHERE status = 'pending'
                        AND channel_type IN ('telegram', 'max')
                        AND (scheduled_at IS NULL OR scheduled_at <= :now)
                        AND (claim_token IS NULL
                             OR lease_expires_at IS NULL
                             OR lease_expires_at < :now)
                      ORDER BY created_at ASC
                      LIMIT :lim
                      FOR UPDATE SKIP LOCKED
                 )
                RETURNING id, tenant_id, created_at
                """
            )
            rows = scan.execute(
                sql,
                {"tok": token, "lease": lease_until, "now": now_utc, "lim": limit},
            ).all()
            scan.commit()
            # RETURNING НЕ гарантирует порядок строк (Codex sol MAJOR): под-SELECT
            # ORDER BY created_at влияет только на то, КАКИЕ строки берутся (LIMIT),
            # но не на порядок RETURNING. Пере-сортируем по (created_at, id), чтобы
            # доставлять в FIFO-порядке постановки — иначе несколько сообщений одному
            # адресату могли уйти в обратном порядке (старый код обрабатывал по created_at).
            rows_sorted = sorted(rows, key=lambda r: (r.created_at, r.id))
            return [(r.id, r.tenant_id) for r in rows_sorted], token

        # SQLite / прочие: клеймим через ORM (без SKIP LOCKED).
        candidates = (
            scan.query(OutboxMessage)
            .filter(
                OutboxMessage.status == "pending",
                OutboxMessage.channel_type.in_(("telegram", "max")),
                or_(
                    OutboxMessage.scheduled_at.is_(None),
                    OutboxMessage.scheduled_at <= now_utc,
                ),
                or_(
                    OutboxMessage.claim_token.is_(None),
                    OutboxMessage.lease_expires_at.is_(None),
                    OutboxMessage.lease_expires_at < now_utc,
                ),
            )
            .order_by(OutboxMessage.created_at.asc())
            .limit(limit)
            .all()
        )
        claimed: list[tuple[str, str]] = []
        for r in candidates:
            r.claim_token = token
            r.lease_expires_at = lease_until
            claimed.append((r.id, r.tenant_id))
        scan.commit()
        return claimed, token

    @staticmethod
    def _release_claim(row: OutboxMessage) -> None:
        """#344 F5 — снять claim со строки, которую оставляем 'pending' (retry
        после ошибки доставки / defer). Без release строку не переклеймят до
        истечения lease (медленный повтор); со release следующий тик подхватит
        её сразу. status не трогаем — остаётся 'pending'."""
        row.claim_token = None
        row.lease_expires_at = None

    # --- delivery failure accounting (audit 2026-07-18 #2) ---------------

    @staticmethod
    def _is_permanent_delivery_error(status_code: int | None) -> bool:
        """HTTP 4xx (кроме 429 rate-limit) = перманентный отказ ЭТОЙ строки:
        повтор с тем же payload бессмыслен (клиенты TG/MAX 4xx сами не
        ретраят — см. integrations/telegram/client.py:307-315).

        2026-07-18 (R1 M15): 401/403 — НЕ перманент для строки, а
        retryable/systemic: отозванный/протухший bot-token (401) и
        «bot blocked/kicked» (403) — состояние КАНАЛА, а не payload'а;
        токен/доступ может восстановиться (перевыпуск, разблок,
        re-add в группу). Строка уходит в обычный retry-цикл с потолком
        OUTBOX_MAX_DELIVERY_ATTEMPTS (≈40–80 мин окно восстановления)
        вместо мгновенного dead-letter."""
        return (
            status_code is not None
            and 400 <= status_code < 500
            and status_code != 429
            and status_code not in (401, 403)
        )

    @staticmethod
    def _clear_attempts(row: OutboxMessage) -> None:
        """Снять in-memory счётчик попыток на терминальном статусе
        (sent/failed/muted/dropped) — иначе словарь копил бы мёртвые id."""
        _DELIVERY_ATTEMPTS.pop(row.id, None)

    def _note_delivery_failure(
        self, row: OutboxMessage, exc: Exception, *, channel: str,
    ) -> str:
        """Аудит 2026-07-18 (#2) — attempts + потолок ретраев + dead-letter.

        Возвращает "dead_letter" (строка терминирована 'failed', claim снят)
        или "retry" (строка остаётся 'pending', claim снят → быстрый ретрай
        на следующем тике, at-least-once).
        """
        status_code = getattr(exc, "status_code", None)
        if self._is_permanent_delivery_error(status_code):
            self._clear_attempts(row)
            row.status = "failed"
            row.drop_reason = f"delivery_permanent_{status_code}"
            self._release_claim(row)
            logger.warning(
                "outbox delivery: permanent %s error on %s (HTTP %s) — "
                "dead-letter (failed/%s)",
                channel, row.id, status_code, row.drop_reason,
            )
            return "dead_letter"
        attempts = _DELIVERY_ATTEMPTS.get(row.id, 0) + 1
        _DELIVERY_ATTEMPTS[row.id] = attempts
        if attempts >= OUTBOX_MAX_DELIVERY_ATTEMPTS:
            self._clear_attempts(row)
            row.status = "failed"
            row.drop_reason = "delivery_retry_exhausted"
            self._release_claim(row)
            logger.error(
                "outbox delivery: %s row %s exhausted %d attempts — "
                "dead-letter (failed/delivery_retry_exhausted)",
                channel, row.id, attempts,
            )
            self._alert_retry_exhausted(row, channel=channel, exc=exc)
            return "dead_letter"
        logger.warning(
            "outbox delivery: %s error on %s (attempt %d/%d) — keeping pending",
            channel, row.id, attempts, OUTBOX_MAX_DELIVERY_ATTEMPTS,
        )
        row.status = "pending"
        self._release_claim(row)
        return "retry"

    @staticmethod
    def _alert_retry_exhausted(
        row: OutboxMessage, *, channel: str, exc: Exception
    ) -> None:
        """Best-effort admin alert о dead-letter после потолка ретраев.
        dedupe_key по КАНАЛУ (не по строке): массовый транзиентный сбой
        даст один алерт на канал за окно rate-limit, а не шторм."""
        try:
            from sreda.services.admin_alerts import send_admin_alert
            # 2026-07-18 (R1 M16): str(exc) у delivery-ошибок может содержать
            # URL/тело ответа с chat_id (канальный PII) — в алерт только
            # класс ошибки + HTTP status, без тела исключения.
            send_admin_alert(
                "P1",
                f"outbox {channel}: retry ceiling reached — row dead-lettered",
                f"row_id={row.id}\nfeature={row.feature_key}\n"
                f"error={type(exc).__name__} "
                f"http_status={getattr(exc, 'status_code', None)}",
                dedupe_key=f"outbox-retry-exhausted:{channel}",
            )
        except Exception:  # noqa: BLE001 — alert не должен ронять доставку
            logger.debug(
                "outbox delivery: retry-exhausted alert failed", exc_info=True,
            )

    async def _process_one(self, session: Session, row: OutboxMessage, *, now_utc: datetime) -> None:
        # #187 soft-delete — fencing (дверь #9): тенант мог быть удалён ПОСЛЕ
        # постановки строки в outbox. Проверяем В ТОЙ ЖЕ TX прямо перед внешней
        # отправкой; удалён → терминируем без send (тот же drop_reason что drain).
        from sreda.services.tenant_lifecycle import is_tenant_active

        if not is_tenant_active(session, row.tenant_id):
            row.status = "dropped"
            row.drop_reason = "tenant_deleted"
            self._clear_attempts(row)  # #2: терминал — снять счётчик попыток
            session.commit()
            return

        profile_dict, skill_config_dict = self._load_user_context(session, row)
        decision = decide_delivery(
            profile=profile_dict,
            skill_config=skill_config_dict,
            feature_key=row.feature_key,
            is_interactive=bool(row.is_interactive),
            now_utc=now_utc,
        )

        if decision.kind == DeliveryKind.drop:
            row.status = "muted"
            self._clear_attempts(row)  # #2: терминал — снять счётчик попыток
            session.commit()
            return
        if decision.kind == DeliveryKind.defer:
            row.scheduled_at = decision.defer_until_utc
            # status stays 'pending'; worker will re-check after defer.
            # #344 F5: снимаем claim — строка снова claimable к моменту defer-выхода.
            self._release_claim(row)
            session.commit()
            return

        # Send path
        await self._send_now(session, row)

    def _load_user_context(
        self, session: Session, row: OutboxMessage
    ) -> tuple[dict | None, dict | None]:
        if not row.user_id:
            return None, None
        repo = UserProfileRepository(session)
        profile = repo.get_profile(row.tenant_id, row.user_id)
        profile_dict: dict | None = None
        if profile is not None:
            profile_dict = {
                "timezone": profile.timezone,
                "quiet_hours": UserProfileRepository.decode_quiet_hours(profile),
            }
        skill_config_dict: dict | None = None
        if row.feature_key:
            config = repo.get_skill_config(
                row.tenant_id, row.user_id, row.feature_key
            )
            if config is not None:
                skill_config_dict = {
                    "notification_priority": config.notification_priority,
                    "token_budget_daily": config.token_budget_daily,
                }
        return profile_dict, skill_config_dict

    async def _send_now(self, session: Session, row: OutboxMessage) -> None:
        # Phase 6 of MAX integration sprint (2026-05-04): branch by channel.
        # MAX и Telegram имеют разные SDK / payload форматы — разносим
        # на отдельные методы. Для будущей расширяемости (e.g. дополнительный
        # канал) этот dispatch расширяется одной веткой.
        if row.channel_type == "max":
            await self._send_now_max(session, row)
            return

        # Default: Telegram path.
        # Phase 4a: resolve client via registry when available so each row
        # is delivered through the bot that originally received the turn.
        # Falls back to self.telegram for backward-compat (legacy wiring,
        # tests that don't pass a registry).
        tg_client: TelegramClient | None
        if self.registry is not None:
            effective_bot_key = row.bot_key or LEGACY_NULL_BOT_KEY
            try:
                tg_client = telegram_client_for(effective_bot_key, self.registry)
            except KeyError:
                if row.bot_key is None:
                    # NULL bot_key: migration-window row — safe to fall back.
                    logger.warning(
                        "outbox delivery: NULL bot_key on row %s, "
                        "falling back to LEGACY_NULL_BOT_KEY",
                        row.id,
                    )
                    tg_client = telegram_client_for(LEGACY_NULL_BOT_KEY, self.registry)
                else:
                    # Explicit unknown key: corruption / stale config — fail
                    # closed, do NOT route through the wrong bot.
                    logger.error(
                        "outbox delivery: unknown bot_key %r on row %s "
                        "(not a NULL legacy row). Marking failed.",
                        row.bot_key,
                        row.id,
                    )
                    row.status = "failed"
                    row.drop_reason = "unknown_bot_key"
                    self._clear_attempts(row)  # #2: терминал
                    session.commit()
                    return
        else:
            tg_client = self.telegram

        if tg_client is None:
            # Dev/test path with no Telegram wired — just mark sent so
            # tests can assert policy without a client mock.
            row.status = "sent"
            session.commit()
            return
        try:
            payload = json.loads(row.payload_json or "{}")
        except json.JSONDecodeError:
            logger.exception("outbox delivery: bad payload_json for %s", row.id)
            row.status = "failed"
            self._clear_attempts(row)  # #2: терминал
            session.commit()
            return

        # Extract end-to-end trace (if the uvicorn process stashed it
        # when enqueuing). Worker emits the final block here after the
        # send attempt so the block lands with the complete timing
        # including delivery latency. Removing it from ``payload`` BEFORE
        # the send means the user-facing Telegram message body doesn't
        # carry our internal bookkeeping — only ``chat_id``/``text``/
        # ``reply_markup``/``parse_mode`` keys are read downstream.
        trace_payload = payload.pop("_trace", None)

        try:
            send_response = await self._send_bounded(
                tg_client.send_message(
                    chat_id=payload.get("chat_id"),
                    text=payload.get("text", ""),
                    reply_markup=payload.get("reply_markup"),
                    parse_mode=payload.get("parse_mode"),
                ),
                TelegramDeliveryError,
            )
            # Stage 9.1: capture TG-side message_id/date for ack-vs-final
            # ordering analysis. См. tomorrow-plan пункт 9.
            tg_msg_id: int | None = None
            tg_date: int | None = None
            result = send_response.get("result") if isinstance(send_response, dict) else None
            if isinstance(result, dict):
                mid = result.get("message_id")
                date_v = result.get("date")
                if isinstance(mid, int):
                    tg_msg_id = mid
                if isinstance(date_v, int):
                    tg_date = date_v

            # Feature-specific post-delivery (e.g. EDS photo sending)
            if row.feature_key:
                hook = get_feature_registry().get_delivery_hook(row.feature_key)
                if hook is not None:
                    try:
                        await hook(
                            session=session,
                            telegram_client=tg_client,
                            outbox_row=row,
                            payload=payload,
                        )
                    except Exception:
                        logger.warning(
                            "outbox delivery: delivery hook failed for %s (feature=%s)",
                            row.id,
                            row.feature_key,
                            exc_info=True,
                        )
            row.status = "sent"
            self._clear_attempts(row)  # #2: терминал — снять счётчик попыток
            # #344 F5 (R1 MINOR): снимаем lease на терминальном 'sent' — иначе
            # partial-index (lease_expires_at IS NOT NULL) копил бы все доставленные.
            self._release_claim(row)
            self._emit_trace(
                trace_payload,
                chat_id=payload.get("chat_id"),
                status="ok",
                tg_message_id=tg_msg_id,
                tg_date=tg_date,
            )
        except TelegramDeliveryError as exc:
            # Аудит 2026-07-18 (#2): attempts + потолок + dead-letter вместо
            # бессрочного pending. Перманентный 4xx → dead-letter сразу;
            # транзиент — ретрай до OUTBOX_MAX_DELIVERY_ATTEMPTS.
            outcome = self._note_delivery_failure(row, exc, channel="telegram")
            if outcome == "dead_letter":
                self._emit_trace(
                    trace_payload,
                    chat_id=payload.get("chat_id"),
                    status="failed",
                )
            # Retry: строка остаётся 'pending', claim снят (#344 F5) →
            # следующий тик подхватит сразу. Trace НЕ эмитим — иначе тот же
            # блок выстрелит повторно на ретрае (idempotency — на свежем
            # контексте, который воркер пересобирает каждый раз).
        except Exception:
            logger.exception("outbox delivery: unexpected error on %s", row.id)
            row.status = "failed"
            self._clear_attempts(row)  # #2: терминал
            self._emit_trace(
                trace_payload,
                chat_id=payload.get("chat_id"),
                status="failed",
            )
        session.commit()

    async def _send_now_max(self, session: Session, row: OutboxMessage) -> None:
        """MAX channel send (Phase 6).

        Payload contract (mirror TG):
        - ``payload.chat_id`` — MAX chat_id (mandatory)
        - ``payload.text`` — message text
        - ``payload.format`` — optional ``markdown``/``html``
        - ``payload.attachments`` — optional list (inline keyboard etc.)

        Trace flow тот же как у TG: ``_emit_trace`` вызывается с
        chat_id и status. Stage 9.1 message_id/date capture — после probe
        Phase 0 знаем что MAX отдаёт ``body.mid`` per message; формат
        capture добавим в follow-up если нужно.
        """
        if self.max is None:
            # 2026-05-05 incident: prod ran без max_client (job_runner
            # forgot to wire его) и rows для МАКС'а тихо помечались
            # 'sent' — Boris думал bot отвечает, по факту message в
            # МАКС не уходил. Чтобы такое больше не повторялось:
            # отличаем dev/test (env без SREDA_MAX_BOT_TOKEN — OK,
            # тестам нужен dev-stub) от prod-misconfig (token задан
            # но клиент не дошёл до worker'а — alert).
            from sreda.config.settings import get_settings as _get_settings
            from sreda.services.admin_alerts import (
                alert_admin_async as _alert,
            )
            if _get_settings().max_bot_token:
                logger.critical(
                    "max outbox %s: max_client=None но "
                    "SREDA_MAX_BOT_TOKEN set — misconfiguration; "
                    "row помечена failed чтобы не врать о delivery",
                    row.id,
                )
                try:
                    await _alert(
                        f"🔴 MAX outbox misconfig: max_client=None в "
                        f"worker'е, row={row.id} помечена failed. "
                        f"Проверь job_runner.py wiring."
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("admin alert failed", exc_info=True)
                row.status = "failed"
            else:
                # Dev/test path — token не настроен в env, mark sent
                # для unit-тестов
                row.status = "sent"
            self._clear_attempts(row)  # #2: терминал
            session.commit()
            return

        try:
            payload = json.loads(row.payload_json or "{}")
        except json.JSONDecodeError:
            logger.exception("max outbox: bad payload_json for %s", row.id)
            row.status = "failed"
            self._clear_attempts(row)  # #2: терминал
            session.commit()
            return

        trace_payload = payload.pop("_trace", None)
        ack_edit_message_id = payload.pop("_ack_edit_message_id", None)
        ack_final_already_visible = bool(
            payload.pop("_ack_final_already_visible", False)
        )
        chat_id = payload.get("chat_id")
        text = payload.get("text", "")

        # Reviewer CRITICAL-1: guard against missing chat_id. Без recipient
        # MAX API всё равно бросит 4xx, но лучше не делать сетевой вызов,
        # пометить failed и оставить trail в логах для диагностики.
        if chat_id is None:
            logger.error(
                "max outbox %s: missing chat_id в payload — "
                "outbound маршрутизация сломана",
                row.id,
            )
            row.status = "failed"
            self._clear_attempts(row)  # #2: терминал
            self._emit_trace(
                trace_payload, chat_id=None, status="failed_no_recipient",
            )
            session.commit()
            return

        # Convert TG-style reply_markup → MAX attachments inline_keyboard.
        # Producers пишут TG schema (исторически), но MAX API ожидает
        # другую structure. Если payload.attachments уже set'нут MAX-native
        # — используем его; иначе конвертим reply_markup. Если reply_markup
        # пустой — text-only message (без кнопок).
        from sreda.integrations.max.client import (
            render_max_inline_keyboard_attachment,
        )
        attachments = payload.get("attachments")
        if not attachments:
            attachments = render_max_inline_keyboard_attachment(
                payload.get("reply_markup"),
            )

        try:
            if ack_edit_message_id:
                if ack_final_already_visible and not attachments:
                    row.status = "sent"
                    self._clear_attempts(row)  # #2: терминал
                    self._emit_trace(
                        trace_payload,
                        chat_id=chat_id,
                        status="sent_ack_already_visible",
                    )
                    session.commit()
                    return
                edit_sent = False
                for attempt in range(2):
                    try:
                        await self.max.edit_message(
                            str(ack_edit_message_id),
                            text=text,
                            attachments=attachments,
                        )
                        edit_sent = True
                        break
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "max outbox: ack edit failed on %s attempt=%d",
                            row.id,
                            attempt + 1,
                            exc_info=True,
                        )
                        if attempt == 0:
                            await asyncio.sleep(0.2)
                if not edit_sent:
                    await self._send_bounded(
                        self.max.send_message(
                            recipient={"chat_id": chat_id},
                            text=text,
                            format=payload.get("format"),
                            attachments=attachments,
                        ),
                        MaxDeliveryError,
                    )
                    try:
                        await self.max.delete_message(str(ack_edit_message_id))
                    except Exception:  # noqa: BLE001
                        logger.debug(
                            "max outbox: fallback ack delete failed on %s mid=%s",
                            row.id,
                            ack_edit_message_id,
                            exc_info=True,
                        )
            else:
                await self._send_bounded(
                    self.max.send_message(
                        recipient={"chat_id": chat_id},
                        text=text,
                        format=payload.get("format"),
                        attachments=attachments,
                    ),
                    MaxDeliveryError,
                )
            row.status = "sent"
            self._clear_attempts(row)  # #2: терминал — снять счётчик попыток
            self._release_claim(row)  # #344 F5 (R1 MINOR): снять lease на терминале
            self._emit_trace(
                trace_payload, chat_id=chat_id, status="ok",
            )
        except MaxDeliveryError as exc:
            # Аудит 2026-07-18 (#2): attempts + потолок + dead-letter вместо
            # бессрочного pending (симметрично TG-пути).
            outcome = self._note_delivery_failure(row, exc, channel="max")
            if outcome == "dead_letter":
                self._emit_trace(
                    trace_payload, chat_id=chat_id, status="failed",
                )
            # Retry: 'pending' + claim снят (#344 F5) → быстрый retry
            # на следующем тике; trace не эмитим (дубль на ретрае).
        except Exception:
            logger.exception("max outbox: unexpected error on %s", row.id)
            row.status = "failed"
            self._clear_attempts(row)  # #2: терминал
            self._emit_trace(
                trace_payload, chat_id=chat_id, status="failed",
            )
        session.commit()

    @staticmethod
    def _emit_trace(
        trace_payload: dict | None,
        *,
        chat_id: object,
        status: str,
        tg_message_id: int | None = None,
        tg_date: int | None = None,
    ) -> None:
        """Render the accumulated end-to-end trace block. No-op if the
        outbox row wasn't tagged with a trace (reminders / EDS
        notifications / other non-conversation rows).

        ``tg_message_id`` / ``tg_date`` (Stage 9.1, см. tomorrow-plan
        пункт 9) — Telegram-side ids возвращённые ``sendMessage``.
        Лежат в final_meta трейса; ``ack.sent`` событие так же содержит
        свой ``tg_message_id`` через ``trace.step``. Сравнение даёт
        диагностику «ack приходит после реплая».
        """
        if not trace_payload:
            return
        try:
            ctx = trace.deserialize_from_outbox(trace_payload)
            final_meta: dict = {
                "chat": str(chat_id) if chat_id is not None else None,
                "status": status,
            }
            if tg_message_id is not None:
                final_meta["tg_message_id"] = tg_message_id
            if tg_date is not None:
                final_meta["tg_date"] = tg_date
            trace.emit_block(
                ctx,
                final_event_name="outbox.delivered",
                final_meta=final_meta,
            )
        except Exception:  # noqa: BLE001 — trace must never kill delivery
            logger.exception("outbox delivery: failed to emit trace block")
