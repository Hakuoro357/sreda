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
secure_records                 7 days      record_type == eds_connect_payload
skill_ai_executions            30 days     any
skill_events (debug/info)      30 days     severity in debug/info
skill_events (warn/error)      90 days     severity in warn/error
skill_run_attempts             90 days     parent run succeeded/failed/cancelled
skill_runs                     90 days     status in succeeded/failed/cancelled
=============================  ==========  ===============================

Order matters: children (attempts, events, ai_executions) are deleted
before their parent runs so we never violate ``skill_run_attempts.run_id``
FK. Everything else uses soft references and order-independence.

Live runs (pending/running/retry_scheduled) are never touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, or_, select, union_all, update
from sqlalchemy.orm import Session

from sreda.db.models.connect import ConnectSession, TenantEDSAccount
from sreda.db.models.core import InboundMessage, Job, OutboxMessage, SecureRecord
from sreda.db.models.runtime import AgentRun
from sreda.db.models.skill_platform import (
    SkillAIExecution,
    SkillEvent,
    SkillRun,
    SkillRunAttempt,
    TenantSkillConfig,
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
    secure_records_eds_connect_payload: int = 0
    skill_ai_executions: int = 0
    skill_events_debug_info: int = 0
    skill_events_warn_error: int = 0
    skill_run_attempts: int = 0
    skill_runs: int = 0
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
            + self.secure_records_eds_connect_payload
            + self.skill_ai_executions
            + self.skill_events_debug_info
            + self.skill_events_warn_error
            + self.skill_run_attempts
            + self.skill_runs
        )


# Retention windows (in days). Kept as module constants so they can be
# patched in tests.
AGENT_RUNS_DAYS = 90
INBOUND_MESSAGES_DAYS = 30
JOBS_DAYS = 30
OUTBOX_SENT_DAYS = 30
OUTBOX_FAILED_DAYS = 60
EDS_CONNECT_PAYLOAD_DAYS = 7
SKILL_AI_EXECUTIONS_DAYS = 30
SKILL_EVENTS_DEBUG_INFO_DAYS = 30
SKILL_EVENTS_WARN_ERROR_DAYS = 90
SKILL_ATTEMPTS_DAYS = 90
SKILL_RUNS_DAYS = 90

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

    # ---------- agent_runs ----------
    agent_runs_cutoff = now - timedelta(days=AGENT_RUNS_DAYS)
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

    # ---------- secure_records (eds_connect_payload only) ----------
    # 2026-04-28 fix: было FK-violation. SecureRecord ссылается из
    # connect_sessions / tenant_eds_accounts / inbound_messages /
    # tenant_skill_configs / skill_runs (in/out) / skill_run_attempts.
    # Удаляем ТОЛЬКО orphan'ов — у которых ни один FK не указывает на них.
    # Если кто-то ещё ссылается — secure_record нужен (parent живой),
    # его TTL обнуляется.
    eds_cutoff = now - timedelta(days=EDS_CONNECT_PAYLOAD_DAYS)
    # union_all через function-form (SQLAlchemy 2.x): chained .union_all
    # на Select возвращает CompoundSelect у которого нет своего .union_all.
    referenced_ids = union_all(
        select(ConnectSession.secure_record_id).where(
            ConnectSession.secure_record_id.isnot(None)
        ),
        select(TenantEDSAccount.secure_record_id).where(
            TenantEDSAccount.secure_record_id.isnot(None)
        ),
        select(InboundMessage.secure_record_id).where(
            InboundMessage.secure_record_id.isnot(None)
        ),
        select(TenantSkillConfig.secure_record_id).where(
            TenantSkillConfig.secure_record_id.isnot(None)
        ),
        select(SkillRun.input_secure_record_id).where(
            SkillRun.input_secure_record_id.isnot(None)
        ),
        select(SkillRun.output_secure_record_id).where(
            SkillRun.output_secure_record_id.isnot(None)
        ),
        select(SkillAIExecution.raw_artifact_secure_record_id).where(
            SkillAIExecution.raw_artifact_secure_record_id.isnot(None)
        ),
    )
    result.secure_records_eds_connect_payload = _delete_returning_count(
        session,
        delete(SecureRecord).where(
            and_(
                SecureRecord.record_type == "eds_connect_payload",
                SecureRecord.created_at < eds_cutoff,
                SecureRecord.id.notin_(referenced_ids),
            )
        ),
    )

    # ---------- plan_library (#135: TTL всем статусам, без PII) ----------
    try:
        from sreda.runtime.planner.plan_library import cleanup_plan_library
        result.plan_library_entries = cleanup_plan_library(session, now=now)
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
