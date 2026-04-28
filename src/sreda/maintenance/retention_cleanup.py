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

from sqlalchemy import and_, delete, or_, select, union_all
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
    inbound_cutoff = now - timedelta(days=INBOUND_MESSAGES_DAYS)
    result.inbound_messages = _delete_returning_count(
        session,
        delete(InboundMessage).where(InboundMessage.created_at < inbound_cutoff),
    )

    # ---------- jobs ----------
    jobs_cutoff = now - timedelta(days=JOBS_DAYS)
    result.jobs = _delete_returning_count(
        session,
        delete(Job).where(
            and_(
                Job.status.in_(TERMINAL_JOB_STATUSES),
                Job.created_at < jobs_cutoff,
            )
        ),
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

    session.flush()
    return result


# Helper so callers (scheduler / CLI) can silence the "unused" warning on ``or_``
# if they re-export the module.
__all__ = [
    "RetentionCleanupResult",
    "cleanup_runtime_retention",
    "or_",  # keep import reachable to future-proof conditional deletes
]
