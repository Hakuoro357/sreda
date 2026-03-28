from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models import AgentRun, AgentThread
from sreda.db.models.core import Job, OutboxMessage
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.runtime.dispatcher import ActionEnvelope
from sreda.services.billing import BillingService

AGENT_EXECUTE_ACTION_JOB = "agent.execute_action"


class ActionRuntimeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class QueuedRuntimeAction:
    thread_id: str
    run_id: str
    job_id: str


@dataclass(frozen=True, slots=True)
class RuntimeReply:
    text: str
    reply_markup: dict | None


class ActionRuntimeService:
    def __init__(
        self,
        session: Session,
        *,
        telegram_client: TelegramClient | None = None,
    ) -> None:
        self.session = session
        self.telegram_client = telegram_client

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
            input_json=json.dumps(action.as_dict(), ensure_ascii=False),
        )
        self.session.add(run)
        job.payload_json = json.dumps({"run_id": run.id}, ensure_ascii=False)
        thread.updated_at = _utcnow()
        self.session.commit()
        return QueuedRuntimeAction(thread_id=thread.id, run_id=run.id, job_id=job.id)

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

        now = _utcnow()
        job.status = "running"
        run.status = "running"
        run.started_at = run.started_at or now
        self.session.commit()

        try:
            action = self._load_action(run)
            context = self._load_context(action)
            handler = self._route_action(action.action_type)
            self._policy_guard(action, context)
            reply = handler(action, context)
            await self._persist_and_enqueue_reply(job=job, run=run, action=action, context=context, reply=reply)
            return "completed"
        except ActionRuntimeError as exc:
            self._mark_failed(job=job, run=run, error_code=exc.code, message=exc.message)
            return "failed"
        except Exception:
            self._mark_failed(
                job=job,
                run=run,
                error_code="runtime_unexpected_error",
                message="Не удалось выполнить действие из-за технической ошибки.",
            )
            return "failed"

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

    def _load_action(self, run: AgentRun) -> ActionEnvelope:
        payload = json.loads(run.input_json or "{}")
        return ActionEnvelope(**payload)

    def _load_context(self, action: ActionEnvelope) -> dict[str, Any]:
        return {
            "tenant_id": action.tenant_id,
            "workspace_id": action.workspace_id,
            "assistant_id": action.assistant_id,
        }

    def _route_action(self, action_type: str):
        handler = {
            "help.show": self._execute_help_show,
            "status.show": self._execute_status_show,
            "subscriptions.show": self._execute_subscriptions_show,
        }.get(action_type)
        if handler is None:
            raise ActionRuntimeError("runtime_unsupported_action", "Это действие пока не поддерживается.")
        return handler

    def _policy_guard(self, action: ActionEnvelope, context: dict[str, Any]) -> None:
        if action.action_type == "help.show":
            return
        if not context.get("tenant_id") or not context.get("workspace_id"):
            raise ActionRuntimeError(
                "runtime_context_missing",
                "Не удалось определить контекст пользователя для этого действия.",
            )

    def _execute_help_show(self, action: ActionEnvelope, context: dict[str, Any]) -> RuntimeReply:
        text, reply_markup = BillingService(self.session).build_help_message()
        return RuntimeReply(text=text, reply_markup=reply_markup)

    def _execute_status_show(self, action: ActionEnvelope, context: dict[str, Any]) -> RuntimeReply:
        text, reply_markup = BillingService(self.session).build_status_message(action.tenant_id)
        return RuntimeReply(text=text, reply_markup=reply_markup)

    def _execute_subscriptions_show(self, action: ActionEnvelope, context: dict[str, Any]) -> RuntimeReply:
        text, reply_markup = BillingService(self.session).build_subscriptions_message(action.tenant_id)
        return RuntimeReply(text=text, reply_markup=reply_markup)

    async def _persist_and_enqueue_reply(
        self,
        *,
        job: Job,
        run: AgentRun,
        action: ActionEnvelope,
        context: dict[str, Any],
        reply: RuntimeReply,
    ) -> None:
        outbox = OutboxMessage(
            id=f"out_{uuid4().hex[:24]}",
            tenant_id=run.tenant_id,
            workspace_id=run.workspace_id,
            channel_type="telegram",
            status="pending",
            payload_json=json.dumps(
                {
                    "chat_id": action.external_chat_id,
                    "text": reply.text,
                    "reply_markup": reply.reply_markup,
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
                    text=reply.text,
                    reply_markup=reply.reply_markup,
                )
                outbox.status = "sent"
            except TelegramDeliveryError:
                outbox.status = "pending"

        now = _utcnow()
        job.status = "completed"
        run.status = "completed"
        run.context_json = json.dumps(context, ensure_ascii=False)
        run.result_json = json.dumps(
            {
                "outbox_message_id": outbox.id,
                "outbox_status": outbox.status,
                "reply_text_preview": reply.text[:120],
            },
            ensure_ascii=False,
        )
        run.finished_at = now
        self.session.commit()

    def _mark_failed(self, *, job: Job, run: AgentRun, error_code: str, message: str) -> None:
        now = _utcnow()
        job.status = "failed"
        run.status = "failed"
        run.error_code = error_code
        run.error_message_sanitized = message
        run.finished_at = now
        self.session.commit()


def _extract_run_id(payload_json: str) -> str | None:
    try:
        payload = json.loads(payload_json or "{}")
    except json.JSONDecodeError:
        return None
    run_id = payload.get("run_id")
    return str(run_id) if run_id else None


def _utcnow() -> datetime:
    return datetime.now(UTC)
