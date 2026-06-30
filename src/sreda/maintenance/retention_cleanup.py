"""Runtime retention cleanup (spec 41 + spec 48 retention mapping).

Single entry point ``cleanup_runtime_retention(session, now=...)`` that
prunes operational-log rows past their retention window. Designed to be
called by a daily scheduled job; safe to run repeatedly.

Deletion rules (from spec 41 / spec 48):

=============================  ==========  ===============================
Table                          Window      Conditions
=============================  ==========  ===============================
agent_runs                     90 days     status in completed/failed
inbound_messages               30 days     any
jobs                           30 days     status in completed/failed/cancelled
outbox_messages (sent)         30 days     status == sent
outbox_messages (failed)       60 days     status == failed
skill_ai_executions            30 days     any
skill_events (debug/info)      30 days     severity in debug/info
skill_events (warn/error)      90 days     severity in warn/error
skill_run_attempts             90 days     parent run succeeded/failed/cancelled
skill_runs                     90 days     status in succeeded/failed/cancelled
=============================  ==========  ===============================

Order matters: children are deleted before their parents so we never
violate non-CASCADE FKs. Two such chains:
  * skill: attempts/events/ai_executions before their parent skill_runs.
  * #164 planner: step_execution_ledger / planner_gaps /
    planner_llm_reservations before planner_executions, and
    planner_executions before its parent agent_runs.
Everything else uses soft references and order-independence.

Live runs (pending/running/retry_scheduled) are never touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from sreda.db.models.core import InboundMessage, Job, OutboxMessage
from sreda.db.models.planner import (
    PlannerExecution,
    PlannerGap,
    PlannerLlmReservation,
    StepExecutionLedger,
)
from sreda.db.models.react_checkpoint import ReactCheckpoint, ReactCheckpointWrite
from sreda.services.react_summary_store import delete_summaries_older_than
from sreda.db.models.react_debug import ReactDebugTurn
from sreda.db.models.react_trace import ReactTurnTrace
from sreda.db.models.runtime import AgentRun
from sreda.db.models.skill_platform import (
    SkillAIExecution,
    SkillEvent,
    SkillRun,
    SkillRunAttempt,
)


@dataclass
class RetentionCleanupResult:
    """Row counts deleted per table. Useful for log/metrics."""

    agent_runs: int = 0
    # #127: прогоны, у которых обнулили ссылку на удаляемое входящее /
    # удаляемый job (не входят в total — это UPDATE, не удаление)
    agent_runs_unlinked: int = 0
    agent_runs_job_unlinked: int = 0
    inbound_messages: int = 0
    jobs: int = 0
    outbox_messages_sent: int = 0
    outbox_messages_failed: int = 0
    outbox_messages_dropped: int = 0  # #187 — drain удалённого тенанта (status='dropped')
    secure_records_eds_connect_payload: int = 0
    skill_ai_executions: int = 0
    skill_events_debug_info: int = 0
    skill_events_warn_error: int = 0
    skill_run_attempts: int = 0
    skill_runs: int = 0
    planner_executions: int = 0  # #164
    step_execution_ledger: int = 0  # #164 (ребёнок planner_executions)
    planner_gaps: int = 0  # #164
    planner_llm_reservations: int = 0  # #164
    react_debug_turns: int = 0  # #185 (временный QA-захват переписки — короткий TTL)
    react_turn_trace: int = 0  # #192 (durable трейс хода — короткий TTL, ПД)
    react_checkpoint: int = 0  # #193 (durable checkpoint диалога — GC по last-activity треда, ПД)
    react_summaries: int = 0  # #232 способ Б (durable-выжимка истории — GC по updated_at, ПД)
    plan_library_entries: int = 0
    deleted_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def total(self) -> int:
        return (
            self.agent_runs
            + self.inbound_messages
            + self.jobs
            + self.outbox_messages_sent
            + self.outbox_messages_failed
            + self.outbox_messages_dropped
            + self.secure_records_eds_connect_payload
            + self.skill_ai_executions
            + self.skill_events_debug_info
            + self.skill_events_warn_error
            + self.skill_run_attempts
            + self.skill_runs
            + self.planner_executions
            + self.step_execution_ledger
            + self.planner_gaps
            + self.planner_llm_reservations
            + self.react_debug_turns
            + self.react_turn_trace
            + self.react_checkpoint
            + self.react_summaries
        )


# Retention windows (in days). Kept as module constants so they can be
# patched in tests.
AGENT_RUNS_DAYS = 90
# #164: planner_executions (с #155 копит зашифрованные ПД) — окно = 90д (как родитель
# agent_runs, утв. владельцем 2026-06-19; max корпус для будущей SIA-петли). Удаляем ТЕРМИНАЛЬНЫЕ
# строки ПЕРЕД чисткой agent_runs (дети раньше родителя — FK run_id NOT NULL, без CASCADE).
PLANNER_EXECUTIONS_DAYS = 90
_PLANNER_LIVE_STATUSES = ("pending", "in_progress")  # живые ходы НЕ чистим
INBOUND_MESSAGES_DAYS = 30
JOBS_DAYS = 30
OUTBOX_SENT_DAYS = 30
OUTBOX_FAILED_DAYS = 60
# #187: outbox со status='dropped' (drain удалённого тенанта). Терминальный
# не-доставленный статус, как 'failed' — то же окно 60д.
OUTBOX_DROPPED_DAYS = 60
SKILL_AI_EXECUTIONS_DAYS = 30
SKILL_EVENTS_DEBUG_INFO_DAYS = 30
SKILL_EVENTS_WARN_ERROR_DAYS = 90
SKILL_ATTEMPTS_DAYS = 90
SKILL_RUNS_DAYS = 90
# #185: react_debug_turns — ВРЕМЕННЫЙ QA-захват переписки (полный текст, EncryptedString). Короткое
# окно: для отлова багов хватает, а ПД всех юзеров не копятся бессрочно (Codex high MAJOR).
REACT_DEBUG_TURNS_DAYS = 14
# #193: react_checkpoint/_write — durable диалог ReAct (ПД, шифр). GC по last-activity ТРЕДА: тред,
# у которого MAX(created_at) < cutoff, удаляется ЦЕЛИКОМ (обе таблицы) — активный тред не режем
# (целостность parent-цепочки). Окно длиннее трейса (живой диалог дольше отладочного следа).
REACT_CHECKPOINT_DAYS = 30
# #192: react_turn_trace — durable трейс хода (контент EncryptedString + структура). Тот же короткий
# TTL, что и debug-захват: наблюдательные данные с ПД не копятся бессрочно.
REACT_TURN_TRACE_DAYS = 14

# #127: размер порции для unlink+delete входящих (см. блок inbound ниже)
INBOUND_CHUNK_SIZE = 5000

TERMINAL_RUN_STATUSES = ("succeeded", "failed", "cancelled")
TERMINAL_JOB_STATUSES = ("completed", "failed", "cancelled")
TERMINAL_AGENT_RUN_STATUSES = ("completed", "failed")


def _delete_returning_count(session: Session, stmt) -> int:
    """Execute a DELETE and return affected-row count.

    ``result.rowcount`` is driver-dependent (SQLite returns it reliably,
    Postgres via psycopg returns it too). We treat ``-1`` / ``None`` as 0
    to stay robust across drivers."""
    result = session.execute(stmt)
    count = getattr(result, "rowcount", 0) or 0
    return max(0, count)


def _unlink_then_delete_chunked(
    session: Session,
    *,
    select_ids,
    make_unlink,
    make_delete,
    chunk_size: int,
) -> tuple[int, int]:
    """#127: удалить строки порциями, предварительно обнулив ссылки на них.

    Паттерн «короткое окно у родителя, длинное у ссылающегося ребёнка»
    (inbound_messages 30д vs agent_runs 90д; jobs 30д vs agent_runs 90д):
    окно родителя соблюдаем строго (PII), ссылка nullable — обнуляем её
    ПЕРЕД удалением. Чанками с commit на каждый: без этого row-локи
    копятся до конца всей чистки (Codex R2), а бэклог после простоя —
    длинная транзакция поверх живого трафика. Частичный прогресс
    безопасен: чистка идемпотентна, повтор доберёт остаток.

    ``select_ids`` — Select по id просроченных строк (с ORDER BY, без
    limit); ``make_unlink(chunk_ids)`` — Update, обнуляющий ссылки;
    ``make_delete(chunk_ids)`` — Delete по чанку.
    Возвращает (unlinked, deleted).
    """
    unlinked_total = 0
    deleted_total = 0
    while True:
        chunk_ids = session.execute(
            select_ids.limit(chunk_size)
        ).scalars().all()
        if not chunk_ids:
            break
        unlinked = session.execute(make_unlink(chunk_ids)).rowcount
        # как в _delete_returning_count: драйвер может вернуть -1/None
        unlinked_total += max(0, unlinked or 0)
        deleted_total += _delete_returning_count(session, make_delete(chunk_ids))
        session.commit()
    return unlinked_total, deleted_total


def cleanup_runtime_retention(
    session: Session,
    *,
    now: datetime | None = None,
) -> RetentionCleanupResult:
    now = now or datetime.now(timezone.utc)
    result = RetentionCleanupResult(deleted_at=now)

    # ---------- skill_ai_executions (before runs) ----------
    ai_cutoff = now - timedelta(days=SKILL_AI_EXECUTIONS_DAYS)
    result.skill_ai_executions = _delete_returning_count(
        session,
        delete(SkillAIExecution).where(SkillAIExecution.created_at < ai_cutoff),
    )

    # ---------- skill_events by severity ----------
    debug_info_cutoff = now - timedelta(days=SKILL_EVENTS_DEBUG_INFO_DAYS)
    result.skill_events_debug_info = _delete_returning_count(
        session,
        delete(SkillEvent).where(
            and_(
                SkillEvent.severity.in_(("debug", "info")),
                SkillEvent.created_at < debug_info_cutoff,
            )
        ),
    )
    warn_error_cutoff = now - timedelta(days=SKILL_EVENTS_WARN_ERROR_DAYS)
    result.skill_events_warn_error = _delete_returning_count(
        session,
        delete(SkillEvent).where(
            and_(
                SkillEvent.severity.in_(("warn", "error")),
                SkillEvent.created_at < warn_error_cutoff,
            )
        ),
    )

    # ---------- skill_run_attempts (before runs, via parent-run filter) ----------
    attempts_cutoff = now - timedelta(days=SKILL_ATTEMPTS_DAYS)
    # delete attempts whose parent run is terminal and older than window
    terminal_old_run_ids = select(SkillRun.id).where(
        and_(
            SkillRun.status.in_(TERMINAL_RUN_STATUSES),
            SkillRun.created_at < attempts_cutoff,
        )
    )
    result.skill_run_attempts = _delete_returning_count(
        session,
        delete(SkillRunAttempt).where(SkillRunAttempt.run_id.in_(terminal_old_run_ids)),
    )

    # ---------- skill_runs ----------
    runs_cutoff = now - timedelta(days=SKILL_RUNS_DAYS)
    result.skill_runs = _delete_returning_count(
        session,
        delete(SkillRun).where(
            and_(
                SkillRun.status.in_(TERMINAL_RUN_STATUSES),
                SkillRun.created_at < runs_cutoff,
            )
        ),
    )

    # ---------- planner_executions + её дети (#164 — дети раньше родителей) ----------
    # FK planner_executions.run_id → agent_runs.id: NOT NULL, без ON DELETE CASCADE. Чтобы чистка
    # agent_runs не падала на FK, удаляем planner_executions ДВУХ видов (Codex R2 — закрыть skew +
    # застрявших-живых): (а) ВСЕ под удаляемыми родителями (run_id ∈ doomed agent_runs) — любой
    # статус/возраст: ребёнок 90д-мёртвого родителя тоже мёртв (вкл. застрявший pending/in_progress
    # и terminal чуть моложе родителя); (б) собственная ретенция planner_executions — терминальные
    # старше своего окна, даже если родитель ещё жив. Недавние живые (свежий родитель) — хранятся.
    #
    # САМА planner_executions — родитель: step_execution_ledger.execution_id (NOT NULL),
    # planner_gaps / planner_llm_reservations (nullable, но non-null ссылки тоже держат) — все FK
    # БЕЗ CASCADE → их детей удаляем ЕЩЁ раньше. Цепочка дети→планнер→agent_runs полная. (Сейчас
    # 3 дочерние таблицы пусты — ledger/billing подключатся следующим срезом #163, но future-safe.)
    # Удаление физически уносит зашифрованные *_enc ПД (нет осиротевших персональных данных).
    agent_runs_cutoff = now - timedelta(days=AGENT_RUNS_DAYS)
    planner_exec_cutoff = now - timedelta(days=PLANNER_EXECUTIONS_DAYS)
    doomed_agent_run_ids = select(AgentRun.id).where(
        and_(
            AgentRun.status.in_(TERMINAL_AGENT_RUN_STATUSES),
            AgentRun.created_at < agent_runs_cutoff,
        )
    )
    _deletable_exec = or_(
        PlannerExecution.run_id.in_(doomed_agent_run_ids),
        and_(
            PlannerExecution.execution_status.notin_(_PLANNER_LIVE_STATUSES),
            PlannerExecution.created_at < planner_exec_cutoff,
        ),
    )
    deletable_exec_ids = select(PlannerExecution.id).where(_deletable_exec)
    result.step_execution_ledger = _delete_returning_count(
        session,
        delete(StepExecutionLedger).where(
            StepExecutionLedger.execution_id.in_(deletable_exec_ids)
        ),
    )
    result.planner_gaps = _delete_returning_count(
        session,
        delete(PlannerGap).where(PlannerGap.execution_id.in_(deletable_exec_ids)),
    )
    result.planner_llm_reservations = _delete_returning_count(
        session,
        delete(PlannerLlmReservation).where(
            PlannerLlmReservation.execution_id.in_(deletable_exec_ids)
        ),
    )
    result.planner_executions = _delete_returning_count(
        session,
        delete(PlannerExecution).where(_deletable_exec),  # тот же предикат (без self-ref)
    )

    # ---------- agent_runs (после её planner-детей → FK-safe) ----------
    result.agent_runs = _delete_returning_count(
        session,
        delete(AgentRun).where(
            and_(
                AgentRun.status.in_(TERMINAL_AGENT_RUN_STATUSES),
                AgentRun.created_at < agent_runs_cutoff,
            )
        ),
    )

    # ---------- inbound_messages ----------
    # #127 (прод 2026-06-09..11): agent_runs живут 90 дней и держат FK
    # inbound_message_id — слепое удаление 30-дневных входящих взрывалось
    # на ссылках живых прогонов (264k ошибок/сутки, чистка стояла трое
    # суток). Окно 30 дней соблюдаем СТРОГО (персональные данные):
    # сначала обнуляем ссылки прогонов (колонка nullable), потом удаляем.
    # Чанками (Codex R1 medium): бэклог после простоя — один мега-UPDATE/
    # DELETE держал бы длинный лок; порциями statement'ы короткие.
    inbound_cutoff = now - timedelta(days=INBOUND_MESSAGES_DAYS)
    result.agent_runs_unlinked, result.inbound_messages = (
        _unlink_then_delete_chunked(
            session,
            select_ids=(
                select(InboundMessage.id)
                .where(InboundMessage.created_at < inbound_cutoff)
                # детерминированный порядок + дружит с индексом created_at
                .order_by(InboundMessage.created_at, InboundMessage.id)
            ),
            make_unlink=lambda ids: (
                update(AgentRun)
                .where(AgentRun.inbound_message_id.in_(ids))
                .values(inbound_message_id=None)
                .execution_options(synchronize_session=False)
            ),
            make_delete=lambda ids: (
                delete(InboundMessage).where(InboundMessage.id.in_(ids))
            ),
            chunk_size=INBOUND_CHUNK_SIZE,
        )
    )

    # ---------- jobs ----------
    # #127 (калибровочный субагент, подтверждено зондом на проде:
    # 556/557 просроченных jobs держались живыми прогонами): тот же
    # FK-класс, что у inbound — agent_runs.job_id (90д) ссылается на jobs
    # (30д). payload_json содержит PII → окно 30 дней строгое, держать
    # job дольше нельзя; колонка job_id nullable — обнуляем перед
    # удалением, прогон выживает.
    jobs_cutoff = now - timedelta(days=JOBS_DAYS)
    result.agent_runs_job_unlinked, result.jobs = _unlink_then_delete_chunked(
        session,
        select_ids=(
            select(Job.id)
            .where(
                and_(
                    Job.status.in_(TERMINAL_JOB_STATUSES),
                    Job.created_at < jobs_cutoff,
                )
            )
            .order_by(Job.created_at, Job.id)
        ),
        make_unlink=lambda ids: (
            update(AgentRun)
            .where(AgentRun.job_id.in_(ids))
            .values(job_id=None)
            .execution_options(synchronize_session=False)
        ),
        make_delete=lambda ids: delete(Job).where(Job.id.in_(ids)),
        chunk_size=INBOUND_CHUNK_SIZE,
    )

    # ---------- outbox_messages ----------
    sent_cutoff = now - timedelta(days=OUTBOX_SENT_DAYS)
    result.outbox_messages_sent = _delete_returning_count(
        session,
        delete(OutboxMessage).where(
            and_(
                OutboxMessage.status == "sent",
                OutboxMessage.created_at < sent_cutoff,
            )
        ),
    )
    failed_cutoff = now - timedelta(days=OUTBOX_FAILED_DAYS)
    result.outbox_messages_failed = _delete_returning_count(
        session,
        delete(OutboxMessage).where(
            and_(
                OutboxMessage.status == "failed",
                OutboxMessage.created_at < failed_cutoff,
            )
        ),
    )
    # #187: status='dropped' (drain удалённого тенанта) — терминальный
    # не-доставленный статус; без этой ветки dropped-строки копились бы
    # бессрочно. То же окно, что failed (60д).
    dropped_cutoff = now - timedelta(days=OUTBOX_DROPPED_DAYS)
    result.outbox_messages_dropped = _delete_returning_count(
        session,
        delete(OutboxMessage).where(
            and_(
                OutboxMessage.status == "dropped",
                OutboxMessage.created_at < dropped_cutoff,
            )
        ),
    )

    # ---------- secure_records (eds_connect_payload) ----------
    # #181 Phase B: EDS Monitor retired — the connect-layer tables
    # (connect_sessions / tenant_eds_accounts) and the eds_connect_payload
    # secure_records they referenced are dropped/deleted by migration
    # 20260622_0060. There is no longer any eds_connect_payload row to clean up
    # here, so this branch is gone. ``result.secure_records_eds_connect_payload``
    # stays at its default 0 for log/metric shape compatibility.

    # ---------- react_debug_turns (#185: временный QA-захват переписки, короткий TTL) ----------
    # Leaf-таблица (нет FK-детей/родителей в чистке), полный текст переписки (EncryptedString).
    # Удаляем строки старше REACT_DEBUG_TURNS_DAYS — ограничиваем накопление ПД при debug_all.
    react_debug_cutoff = now - timedelta(days=REACT_DEBUG_TURNS_DAYS)
    result.react_debug_turns = _delete_returning_count(
        session,
        delete(ReactDebugTurn).where(ReactDebugTurn.created_at < react_debug_cutoff),
    )

    # #192: react_turn_trace — durable трейс хода, тот же короткий TTL (ПД).
    react_trace_cutoff = now - timedelta(days=REACT_TURN_TRACE_DAYS)
    result.react_turn_trace = _delete_returning_count(
        session,
        delete(ReactTurnTrace).where(ReactTurnTrace.created_at < react_trace_cutoff),
    )

    # #193: react_checkpoint/_write — durable диалог. GC по last-activity ТРЕДА: тред, у которого
    # MAX(created_at) < cutoff, удаляем ЦЕЛИКОМ (обе таблицы); активный тред не трогаем (целостность
    # parent-цепочки checkpoint'ов). Одной транзакцией (как и вся чистка).
    cp_cutoff = now - timedelta(days=REACT_CHECKPOINT_DAYS)
    stale_threads = session.execute(
        select(ReactCheckpoint.thread_id, ReactCheckpoint.checkpoint_ns)
        .group_by(ReactCheckpoint.thread_id, ReactCheckpoint.checkpoint_ns)
        .having(func.max(ReactCheckpoint.created_at) < cp_cutoff)
    ).all()
    for thread_id, ns in stale_threads:
        session.execute(
            delete(ReactCheckpointWrite).where(
                ReactCheckpointWrite.thread_id == thread_id,
                ReactCheckpointWrite.checkpoint_ns == ns,
            )
        )
        result.react_checkpoint += _delete_returning_count(
            session,
            delete(ReactCheckpoint).where(
                ReactCheckpoint.thread_id == thread_id,
                ReactCheckpoint.checkpoint_ns == ns,
            ),
        )

    # #232 способ Б: react_summaries — durable-выжимка истории (ПД, шифр), лист-таблица (нет FK-детей).
    # GC по updated_at тем же окном, что тред (#193). Единый источник правды — store (не дублируем delete).
    summary_cutoff = now - timedelta(days=REACT_CHECKPOINT_DAYS)
    result.react_summaries = delete_summaries_older_than(session, summary_cutoff)

    # ---------- plan_library (#135: TTL всем статусам, без PII) ----------
    try:
        from sreda.runtime.planner.plan_library import cleanup_plan_library
        # SAVEPOINT (Codex R1 high): голый try оставил бы транзакцию
        # PG в aborted-состоянии — последующий flush упал бы
        with session.begin_nested():
            result.plan_library_entries = cleanup_plan_library(
                session, now=now)
    except Exception:  # noqa: BLE001 — новая таблица не валит чистку
        import logging
        logging.getLogger(__name__).warning(
            "plan_library cleanup failed", exc_info=True)

    session.flush()
    return result


# Helper so callers (scheduler / CLI) can silence the "unused" warning on ``or_``
# if they re-export the module.
__all__ = [
    "RetentionCleanupResult",
    "cleanup_runtime_retention",
    "or_",  # keep import reachable to future-proof conditional deletes
]
