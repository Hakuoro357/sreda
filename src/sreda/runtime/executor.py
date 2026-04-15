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
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session

from sreda.config.settings import get_settings
from sreda.db.models import AgentRun, AgentThread
from sreda.db.models.core import Job, OutboxMessage
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.runtime.dispatcher import ActionEnvelope
from sreda.runtime.graph import get_assistant_graph, sanitize_error_message
from sreda.runtime.handlers import ActionRuntimeError, RuntimeReply  # re-export
from sreda.services.privacy_guard import get_default_privacy_guard

__all__ = [
    "ActionRuntimeError",
    "ActionRuntimeService",
    "AGENT_EXECUTE_ACTION_JOB",
    "QueuedRuntimeAction",
    "RuntimeReply",
]

logger = logging.getLogger(__name__)

AGENT_EXECUTE_ACTION_JOB = "agent.execute_action"


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


class ActionRuntimeService:
    def __init__(
        self,
        session: Session,
        *,
        telegram_client: TelegramClient | None = None,
    ) -> None:
        self.session = session
        self.telegram_client = telegram_client
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
            input_json=json.dumps(
                get_default_privacy_guard().sanitize_structure(action.as_dict()).sanitized_value,
                ensure_ascii=False,
            ),
        )
        self.session.add(run)
        job.payload_json = json.dumps({"run_id": run.id}, ensure_ascii=False)
        thread.updated_at = _utcnow()
        self.session.commit()
        return QueuedRuntimeAction(thread_id=thread.id, run_id=run.id, job_id=job.id)

    # --------------------------------------------------------- batch runner

    async def process_pending_jobs(self, *, limit: int = 20) -> int:
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
        return "failed" if outcome == "failed" else "completed"

    # ------------------------------------------------------------- helpers

    def _get_or_create_thread(self, action: ActionEnvelope) -> AgentThread:
        thread = (
            self.session.query(AgentThread)
            .filter(
                AgentThread.tenant_id == action.tenant_id,
                AgentThread.channel_type == action.channel_type,
                AgentThread.external_chat_id == action.external_chat_id,
            )
            .one_or_none()
        )
        if thread is not None:
            return thread

        thread = AgentThread(
            id=f"thread_{uuid4().hex[:24]}",
            tenant_id=action.tenant_id,
            workspace_id=action.workspace_id,
            assistant_id=action.assistant_id,
            channel_type=action.channel_type,
            external_chat_id=action.external_chat_id,
            status="active",
        )
        self.session.add(thread)
        self.session.flush()
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
            channel_type="telegram",
            status="pending",
            payload_json=json.dumps(
                {
                    "chat_id": action.external_chat_id,
                    "text": sanitized_message,
                    "reply_markup": reply_markup,
                },
                ensure_ascii=False,
            ),
        )
        self.session.add(outbox)
        self.session.flush()

        if self.telegram_client is not None:
            try:
                await self.telegram_client.send_message(
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
        self.session.commit()
