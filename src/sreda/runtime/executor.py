"""Action runtime service — thin wrapper around the assistant graph.

Responsibilities that live here (NOT in the graph):

  * ``enqueue_action`` — create the ``Job`` + ``AgentRun`` rows and the
    owning ``AgentThread`` so a webhook handler can fire-and-forget.
  * ``process_job`` — load the run, CAS-claim the job row
    (``pending`` → ``running`` under row-level lock to block duplicate
    workers), and hand off to the compiled graph.
  * Wall-clock timeout around the async graph invocation: a hung upstream
    (Telegram send, etc.) must not pin a job in ``running`` forever.
  * Best-effort failure finalization if the graph itself raises or times
    out mid-flight — the graph's ``persist_error`` node normally handles
    this, but if it never ran (timeout), we still need the DB row to
    leave ``running``.

Everything else — context loading, policy, dispatch, reply persistence,
Telegram side-effects — is inside the graph (``sreda.runtime.graph``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sreda.config.bot_registry import TelegramBotRegistry, telegram_client_for
from sreda.config.settings import get_settings
from sreda.db.models import AgentRun, AgentThread
from sreda.db.models.core import Job, OutboxMessage
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.runtime.dispatcher import ActionEnvelope
from sreda.runtime.graph import get_assistant_graph, sanitize_error_message
from sreda.runtime.handlers import ActionRuntimeError, RuntimeReply  # re-export

__all__ = [
    "ActionRuntimeError",
    "ActionRuntimeService",
    "AGENT_EXECUTE_ACTION_JOB",
    "QueuedRuntimeAction",
    "RuntimeReply",
]

logger = logging.getLogger(__name__)

AGENT_EXECUTE_ACTION_JOB = "agent.execute_action"

# ---------------------------------------------------------------------
# Stale-running reaper (audit 2026-07-18: runtime-core #2,
# cross-concurrency П2/Н2). Процесс, умерший между CAS-claim и
# финализацией (deploy-рестарт, OOM, SIGKILL), раньше оставлял Job и
# AgentRun в ``running`` НАВСЕГДА — reaper'а в пакете не было, только
# детекторы (crash_alert_monitor / reliability_report).
#
# Форма — по эталону ``workers/message_dispatcher.py`` (lease +
# settle-window, НЕ мгновенная терминализация, Н2): запись терминализируется
# только если она увидена застрявшей на ДВУХ sweep'ах с интервалом ≥
# ``settle_seconds``. Это защищает медленный-но-живой job (чья sync
# DB-работа может пережить asyncio wall-clock timeout) от убийства в
# полёте — иначе «потерянное сообщение» превратилось бы в «дубль
# сообщения» + двойное списание free-tier.

_REAPER_FIRST_SEEN: dict[str, datetime] = {}
"""job_id → когда sweep впервые увидел его застрявшим. Module-level
нарочно: ``ActionRuntimeService`` пересоздаётся на каждый тик job_runner
и на каждый webhook — instance-state не доживёт до второго sighting.
Process-local by design: после рестарта settle-окно просто начинается
заново (fail-safe в сторону НЕ reap'ить)."""

_REAPER_MARKS_CAP = 1000
"""Жёсткий потолок на settle-марки — сам dict тоже bounded."""


@dataclass(frozen=True, slots=True)
class QueuedRuntimeAction:
    thread_id: str
    run_id: str
    job_id: str


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _make_thread_id(action: ActionEnvelope, run_id: str) -> str:
    """Checkpointer thread identity.

    Uses ``run_id`` so each invocation is isolated from previous runs on
    the same (tenant, channel, chat). Phase 3's long-term memory will
    live in a separate LangMem store — NOT in graph channel-state — so
    we don't need cross-invocation continuity here. Sharing thread_id
    across invocations would leak stale ``error_code`` / ``replies``
    from prior runs into the new one (LangGraph's ``last_value``
    channels preserve fields not re-set by the new input)."""
    _ = action  # kept for future when we want hierarchical thread ids
    return run_id


def _extract_run_id(payload_json: str) -> str | None:
    try:
        payload = json.loads(payload_json or "{}")
    except json.JSONDecodeError:
        return None
    run_id = payload.get("run_id")
    return str(run_id) if run_id else None


def _hash_chat_id(value: object) -> str:
    """2026-07-18 (R1 C4): external_chat_id — внешний идентификатор чата (PII).
    В логи пишем короткий sha256-префикс, не сырое значение."""
    return "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _redact_run_input_json(run: AgentRun) -> None:
    """2026-07-18 (R1 C4): run терминален (completed/failed/reaped) — сырой
    input_json больше не нужен для исполнения (реплей completed отсекается в
    process_job, failed/reaped не переисполняются). Санитайзим PII-токены
    (URL/креды/e-mail/мед-данные) at-rest через privacy_guard: обычный
    текст диалога сохраняется (history по params.text работает), но секреты
    больше не лежат в plaintext-колонке весь срок жизни AgentRun.
    Best-effort — не роняем финализацию run'а."""
    try:
        raw = run.input_json
        if not raw or raw == "{}":
            return
        from sreda.services.privacy_guard import get_default_privacy_guard

        data = json.loads(raw)
        redacted = get_default_privacy_guard().sanitize_structure(data).sanitized_value
        run.input_json = json.dumps(redacted, ensure_ascii=False)
    except Exception:  # noqa: BLE001 — редактирование PII не должно валить run
        logger.exception(
            "runtime: failed to redact run input_json for %s",
            getattr(run, "id", "?"),
        )


class ActionRuntimeService:
    def __init__(
        self,
        session: Session,
        *,
        telegram_client: TelegramClient | None = None,
        bot_registry: TelegramBotRegistry | None = None,
        llm_client: object | None = None,
        embedding_client: object | None = None,
        ack_progress_controller: object | None = None,
    ) -> None:
        self.session = session
        self.telegram_client = telegram_client
        # When ``bot_registry`` is supplied, inline sends resolve the client
        # per ``action.bot_key`` so a sreda_home turn is delivered by the
        # sreda_home bot, not the default one.  Tests that inject a single
        # fake client can leave ``bot_registry=None`` — the single-client
        # fallback still works.
        self.bot_registry = bot_registry
        # ``llm_client`` / ``embedding_client`` are optional — when
        # ``None``, handlers fall through to the settings-based factories
        # (``get_chat_llm`` / ``get_embeddings_client``). Tests inject
        # fakes here to avoid live provider calls.
        self.llm_client = llm_client
        self.embedding_client = embedding_client
        self.ack_progress_controller = ack_progress_controller
        self._graph = get_assistant_graph()

    # ------------------------------------------------------------- enqueue

    def enqueue_action(self, action: ActionEnvelope) -> QueuedRuntimeAction:
        thread = self._get_or_create_thread(action)
        job = Job(
            id=f"job_{uuid4().hex[:24]}",
            tenant_id=action.tenant_id,
            workspace_id=action.workspace_id,
            job_type=AGENT_EXECUTE_ACTION_JOB,
            status="pending",
            payload_json="{}",
        )
        self.session.add(job)
        self.session.flush()

        run = AgentRun(
            id=f"run_{uuid4().hex[:24]}",
            thread_id=thread.id,
            tenant_id=action.tenant_id,
            workspace_id=action.workspace_id,
            assistant_id=action.assistant_id,
            job_id=job.id,
            inbound_message_id=action.inbound_message_id,
            action_type=action.action_type,
            status="pending",
            # Audit 2026-07-18 (runtime-core #1): persist the ORIGINAL
            # envelope and execute it as-is. Until this fix the row stored
            # a privacy-guard-sanitized copy and ``process_job`` executed
            # THAT — deterministic corruption of user input («прочитай
            # https://site.ru/page?id=7» → «прочитай [url]» мёртвый
            # fetch_url; «у меня аллергия…» → плейсхолдеры в core-памяти).
            # Sanitization остаётся на путях, где она и проектировалась:
            # error-тексты (``sanitize_error_message``) — не на hot-path
            # исполнения action.
            input_json=json.dumps(action.as_dict(), ensure_ascii=False),
        )
        self.session.add(run)
        job.payload_json = json.dumps({"run_id": run.id}, ensure_ascii=False)
        thread.updated_at = _utcnow()
        self.session.commit()
        return QueuedRuntimeAction(thread_id=thread.id, run_id=run.id, job_id=job.id)

    # --------------------------------------------------------- batch runner

    async def process_pending_jobs(self, *, limit: int = 20) -> int:
        # Sweep застрявших ``running`` ПЕРЕД свежей работой, чтобы краш
        # предшественника не ждал ручной правки БД (runtime-core #2).
        # Reaper сам ловит свои исключения — тик не умирает.
        await self.reap_stale_running_jobs()
        jobs = (
            self.session.query(Job)
            .filter(Job.job_type == AGENT_EXECUTE_ACTION_JOB, Job.status == "pending")
            .order_by(Job.id.asc())
            .limit(limit)
            .all()
        )
        processed = 0
        for job in jobs:
            await self.process_job(job.id)
            processed += 1
        return processed

    # ----------------------------------------------------- stale reaper

    async def reap_stale_running_jobs(
        self,
        *,
        limit: int = 50,
        stale_after_seconds: float | None = None,
        settle_seconds: float | None = None,
        now: datetime | None = None,
    ) -> int:
        """Finalize ``agent.execute_action`` jobs застрявшие в ``running``.

        Stale-анкер — ``AgentRun.started_at`` (проставляется при claim):
        run в ``running`` со ``started_at`` старше ``stale_after_seconds``
        (по умолчанию ``max(2 × job_max_runtime_seconds, 300)`` — живой
        job ограничен wall-clock timeout'ом и не может быть старше).
        Терминализация — только после settle-окна (второй sighting через
        ≥ ``settle_seconds``, по умолчанию ``max(job_max_runtime_seconds,
        300)``), по эталону message_dispatcher lease + Н2.

        Возвращает число записей, реально терминализированных на ЭТОМ
        sweep'е. Никогда не поднимает исключений — recovery не должен
        ронять тик job_runner.
        """
        now = now or _utcnow()
        timeout = max(get_settings().job_max_runtime_seconds, 0.001)
        stale_after = (
            stale_after_seconds
            if stale_after_seconds is not None
            else max(2.0 * timeout, 300.0)
        )
        settle = (
            settle_seconds
            if settle_seconds is not None
            else max(timeout, 300.0)
        )
        cutoff = now - timedelta(seconds=stale_after)

        try:
            rows = (
                self.session.query(Job, AgentRun)
                .join(AgentRun, AgentRun.job_id == Job.id)
                .filter(
                    Job.job_type == AGENT_EXECUTE_ACTION_JOB,
                    Job.status == "running",
                    AgentRun.status == "running",
                    AgentRun.started_at.is_not(None),
                    AgentRun.started_at < cutoff,
                )
                .order_by(Job.id.asc())
                .limit(limit)
                .all()
            )
        except Exception:
            self.session.rollback()
            logger.exception("runtime: stale-running sweep query failed")
            return 0

        stale_ids = {job.id for job, _run in rows}
        reaped = 0
        for job, run in rows:
            first_seen = _REAPER_FIRST_SEEN.get(job.id)
            if first_seen is None:
                _REAPER_FIRST_SEEN[job.id] = now
                logger.warning(
                    "runtime: stale running job %s (run %s, started_at=%s) — "
                    "settle window opened; will reap if still running on a "
                    "later sweep",
                    job.id, run.id, run.started_at,
                )
                continue
            if (now - first_seen).total_seconds() < settle:
                continue
            reaped += self._reap_stale_job(job_id=job.id, run_id=run.id, now=now)

        # Марки записей, покинувших stale-set (поздний живой владелец
        # финализировал сам или мы только что reap'нули) — снимаем, затем
        # жёсткий потолок на размер dict'а.
        for marked_id in list(_REAPER_FIRST_SEEN):
            if marked_id not in stale_ids:
                _REAPER_FIRST_SEEN.pop(marked_id, None)
        if len(_REAPER_FIRST_SEEN) > _REAPER_MARKS_CAP:
            oldest_first = sorted(
                _REAPER_FIRST_SEEN.items(), key=lambda kv: kv[1]
            )
            for marked_id, _seen in oldest_first[
                : len(_REAPER_FIRST_SEEN) - _REAPER_MARKS_CAP
            ]:
                _REAPER_FIRST_SEEN.pop(marked_id, None)
        return reaped

    def _reap_stale_job(self, *, job_id: str, run_id: str, now: datetime) -> int:
        """Идемпотентная CAS-финализация одной застрявшей пары job/run.

        Оба UPDATE условные (``status='running'``) — если поздний-живой
        владелец или конкурентный sweeper финализировал первым, это no-op
        (Н2: идемпотентная финализация с первого дня)."""
        try:
            claim = self.session.execute(
                update(Job)
                .where(Job.id == job_id)
                .where(Job.status == "running")
                .values(status="failed")
            )
            if claim.rowcount != 1:
                self.session.rollback()
                return 0
            # M3 (R1): требовать rowcount==1 и от AgentRun-апдейта. Иначе
            # (run уже терминализирован живым владельцем: completed) мы бы
            # закоммитили Job=failed поверх run=completed — рассинхрон.
            # C4 (R1): reaped-run исполнять уже не будут → чистим сырой
            # input_json (PII) прямо в условном апдейте.
            run_claim = self.session.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .where(AgentRun.status == "running")
                .values(
                    status="failed",
                    error_code="runtime_stale_reaped",
                    error_message_sanitized=sanitize_error_message(
                        "Действие не завершилось: обработчик был перезапущен."
                    ),
                    finished_at=now,
                    input_json="{}",
                )
            )
            if run_claim.rowcount != 1:
                self.session.rollback()
                return 0
            self.session.commit()
        except Exception:
            self.session.rollback()
            logger.exception("runtime: failed to reap stale job %s", job_id)
            return 0
        logger.error(
            "runtime: reaped stale running job %s (run %s) — процесс умер "
            "между claim и finalize; сообщение НЕ будет обработано "
            "повторно автоматически (audit 2026-07-18 runtime-core #2)",
            job_id, run_id,
        )
        return 1

    # ------------------------------------------------------------- process

    async def process_job(self, job_id: str) -> str:
        job = self.session.get(Job, job_id)
        if job is None:
            return "missing"
        if job.job_type != AGENT_EXECUTE_ACTION_JOB:
            return "skipped"
        if job.status == "completed":
            return "completed"
        if job.status not in {"pending", "running"}:
            return "skipped"

        run_id = _extract_run_id(job.payload_json)
        if run_id is None:
            job.status = "failed"
            self.session.commit()
            return "failed"

        run = self.session.get(AgentRun, run_id)
        if run is None:
            job.status = "failed"
            self.session.commit()
            return "failed"
        if run.status == "completed":
            job.status = "completed"
            self.session.commit()
            return "completed"

        # CAS-claim: atomically flip ``status`` from ``pending`` → ``running``
        # so concurrent workers race on a row-level write. The loser rolls
        # back and bails out before any side-effect runs.
        claim = self.session.execute(
            update(Job)
            .where(Job.id == job.id)
            .where(Job.status == "pending")
            .values(status="running")
        )
        if claim.rowcount != 1:
            self.session.rollback()
            return "claimed_by_other"
        self.session.expire(job)
        now = _utcnow()
        run.status = "running"
        run.started_at = run.started_at or now
        self.session.commit()

        timeout_seconds = max(get_settings().job_max_runtime_seconds, 0.001)

        try:
            action = ActionEnvelope(**json.loads(run.input_json or "{}"))
        except Exception:
            logger.exception("runtime: failed to decode action envelope for run %s", run.id)
            await self._finalize_unexpected_failure(
                job_id=job.id,
                run_id=run.id,
                error_code="runtime_unexpected_error",
            )
            return "failed"

        thread_id = _make_thread_id(action, run.id)
        graph_input = {
            "action": action.as_dict(),
            "run_id": run.id,
            "job_id": job.id,
        }
        graph_config = {
            "configurable": {
                "thread_id": thread_id,
                "session": self.session,
                "telegram_client": self.telegram_client,
                "bot_registry": self.bot_registry,
                "llm_client": self.llm_client,
                "embedding_client": self.embedding_client,
                "ack_progress_controller": self.ack_progress_controller,
            }
        }

        try:
            final_state = await asyncio.wait_for(
                self._graph.ainvoke(graph_input, config=graph_config),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            self.session.rollback()
            await self._finalize_timeout(
                job_id=job.id,
                run_id=run.id,
                action=action,
                timeout_seconds=timeout_seconds,
            )
            return "failed"
        except Exception:
            logger.exception("runtime: graph raised for run %s", run.id)
            self.session.rollback()
            await self._finalize_unexpected_failure(
                job_id=job.id,
                run_id=run.id,
                action=action,
                error_code="runtime_unexpected_error",
            )
            return "failed"

        outcome = (final_state or {}).get("outcome")
        # C4 (R1): граф завершил run (completed/failed) и закоммитил. Сырой
        # input_json больше не нужен для исполнения — санитайзим PII at-rest.
        try:
            terminal_run = self.session.get(AgentRun, run_id)
            if terminal_run is not None:
                _redact_run_input_json(terminal_run)
                self.session.commit()
        except Exception:  # noqa: BLE001 — редактирование не должно менять исход
            self.session.rollback()
            logger.exception(
                "runtime: post-run input_json redaction failed for run %s", run_id
            )
        return "failed" if outcome == "failed" else "completed"

    # ------------------------------------------------------------- helpers

    def _get_or_create_thread(self, action: ActionEnvelope) -> AgentThread:
        # Audit 2026-07-18 (runtime-core #5, cross-concurrency FC-2):
        # раньше уникальности по (tenant_id, channel_type,
        # external_chat_id) не было — конкурентный enqueue создавал
        # дубль, после чего ``one_or_none()`` падал MultipleResultsFound
        # на КАЖДОМ следующем enqueue (мёртвый чат до ручной чистки БД).
        # DB-констрейнт теперь добавлен db-uniques (миграция
        # 20260718_0085), но FC-2 требует tolerant readers вторым слоем:
        # БД, мигрированная с УЖЕ существующими дублями, не должна
        # умирать. Берём СТАРЕЙШИЙ thread (на него уже ссылаются
        # существующие runs) + громкий alert на дубль.
        threads = (
            self.session.query(AgentThread)
            .filter(
                AgentThread.tenant_id == action.tenant_id,
                AgentThread.channel_type == action.channel_type,
                AgentThread.external_chat_id == action.external_chat_id,
            )
            .order_by(AgentThread.created_at.asc(), AgentThread.id.asc())
            .limit(2)
            .all()
        )
        if len(threads) > 1:
            logger.error(
                "runtime: duplicate AgentThread rows tenant=%s channel=%s "
                "chat=%s ids=%s — using oldest %s; needs DB cleanup + "
                "unique constraint (FC-2 / db-uniques)",
                action.tenant_id,
                action.channel_type,
                _hash_chat_id(action.external_chat_id),  # C4 (R1): PII → hash
                [t.id for t in threads],
                threads[0].id,
            )
        if threads:
            return threads[0]

        # M4 (R1): создание thread'а — get-or-create с гонкой. Конкурентный
        # enqueue того же (tenant, channel, chat) выиграл бы INSERT, а наш
        # flush упал бы на uq_agent_threads_tenant_channel_chat (миграция
        # 0085) → action терялся. Savepoint изолирует конфликт от внешней
        # транзакции; на IntegrityError переспрашиваем победителя (образец
        # planner/persistence.insert_or_resume).
        thread = AgentThread(
            id=f"thread_{uuid4().hex[:24]}",
            tenant_id=action.tenant_id,
            workspace_id=action.workspace_id,
            assistant_id=action.assistant_id,
            channel_type=action.channel_type,
            external_chat_id=action.external_chat_id,
            status="active",
        )
        try:
            with self.session.begin_nested():
                self.session.add(thread)
                self.session.flush()
        except IntegrityError:
            winner = (
                self.session.query(AgentThread)
                .filter(
                    AgentThread.tenant_id == action.tenant_id,
                    AgentThread.channel_type == action.channel_type,
                    AgentThread.external_chat_id == action.external_chat_id,
                )
                .order_by(AgentThread.created_at.asc(), AgentThread.id.asc())
                .first()
            )
            if winner is None:
                # UNIQUE-конфликт не по нашей тройке — что-то иное, пробрасываем.
                raise
            return winner
        return thread

    async def _finalize_timeout(
        self,
        *,
        job_id: str,
        run_id: str,
        action: ActionEnvelope,
        timeout_seconds: float,
    ) -> None:
        """Graph timed out mid-flight — persist ``failed`` state + best-effort
        user notification. The notification itself is bounded by the same
        budget so a hung Telegram client can't re-pin the job."""
        message = "Действие отменено по таймауту."
        sanitized = sanitize_error_message(message)
        try:
            await asyncio.wait_for(
                self._write_failure_with_notification(
                    job_id=job_id,
                    run_id=run_id,
                    action=action,
                    error_code="runtime_timeout",
                    sanitized_message=sanitized,
                    reply_markup=None,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            # Notification hung too — drop to a bare DB finalize so the
            # job row at least leaves ``running``.
            self.session.rollback()
            self._write_failure_row_only(
                job_id=job_id,
                run_id=run_id,
                error_code="runtime_timeout",
                sanitized_message=sanitized,
            )

    async def _finalize_unexpected_failure(
        self,
        *,
        job_id: str,
        run_id: str,
        action: ActionEnvelope | None = None,
        error_code: str,
    ) -> None:
        message = "Не удалось выполнить действие из-за технической ошибки."
        sanitized = sanitize_error_message(message)
        if action is None:
            self._write_failure_row_only(
                job_id=job_id,
                run_id=run_id,
                error_code=error_code,
                sanitized_message=sanitized,
            )
            return
        try:
            await self._write_failure_with_notification(
                job_id=job_id,
                run_id=run_id,
                action=action,
                error_code=error_code,
                sanitized_message=sanitized,
                reply_markup=None,
            )
        except Exception:
            logger.exception("runtime: failed to persist unexpected-failure state")
            self.session.rollback()
            self._write_failure_row_only(
                job_id=job_id,
                run_id=run_id,
                error_code=error_code,
                sanitized_message=sanitized,
            )

    async def _write_failure_with_notification(
        self,
        *,
        job_id: str,
        run_id: str,
        action: ActionEnvelope,
        error_code: str,
        sanitized_message: str,
        reply_markup: dict | None,
    ) -> None:
        job = self.session.get(Job, job_id)
        run = self.session.get(AgentRun, run_id)
        if job is None or run is None:
            return

        outbox = OutboxMessage(
            id=f"out_{uuid4().hex[:24]}",
            tenant_id=run.tenant_id,
            workspace_id=run.workspace_id,
            # Derived from action — see ActionEnvelope.outbox_channel.
            # MAX failure-notifications идут через worker (status='pending'),
            # TG inline.
            channel_type=action.outbox_channel,
            status="pending",
            payload_json=json.dumps(
                {
                    "chat_id": action.external_chat_id,
                    "text": sanitized_message,
                    "reply_markup": reply_markup,
                },
                ensure_ascii=False,
            ),
            # Phase 4a: carry the turn's bot_key so the delivery worker
            # routes via the correct TelegramClient.
            bot_key=action.bot_key,
        )
        self.session.add(outbox)
        self.session.flush()

        # Inline-send только для telegram (см. graph.py rationale). MAX
        # failure rows подхватит OutboxDeliveryWorker._send_now_max.
        # Resolve the correct client per action.bot_key when a registry is
        # available; fall back to the injected single client for tests /
        # backward-compat callers that pass bot_registry=None.
        if action.outbox_channel == "telegram":
            if self.bot_registry is not None:
                _tg_client = telegram_client_for(action.bot_key, self.bot_registry)
            else:
                _tg_client = self.telegram_client
            if _tg_client is not None:
                try:
                    await _tg_client.send_message(
                        chat_id=action.external_chat_id,
                        text=sanitized_message,
                        reply_markup=reply_markup,
                    )
                    outbox.status = "sent"
                except TelegramDeliveryError:
                    outbox.status = "pending"

        now = _utcnow()
        job.status = "failed"
        run.status = "failed"
        run.result_json = json.dumps(
            {
                "outbox_message_ids": [outbox.id],
                "outbox_statuses": [outbox.status],
                "reply_count": 1,
            },
            ensure_ascii=False,
        )
        run.error_code = error_code
        run.error_message_sanitized = sanitized_message
        run.finished_at = now
        _redact_run_input_json(run)  # C4 (R1): run терминален — санитайз PII
        self.session.commit()

    def _write_failure_row_only(
        self,
        *,
        job_id: str,
        run_id: str,
        error_code: str,
        sanitized_message: str,
    ) -> None:
        job = self.session.get(Job, job_id)
        run = self.session.get(AgentRun, run_id)
        if job is not None and job.status != "failed":
            job.status = "failed"
        if run is not None:
            run.status = "failed"
            run.error_code = error_code
            run.error_message_sanitized = sanitized_message
            run.finished_at = _utcnow()
            _redact_run_input_json(run)  # C4 (R1): run терминален — санитайз PII
        self.session.commit()
