from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.config.settings import get_settings
from sreda.db.models import AgentRun, AgentThread
from sreda.db.models.core import Job, OutboxMessage, TenantFeature
from sreda.integrations.telegram.client import TelegramClient, TelegramDeliveryError
from sreda.runtime.dispatcher import ActionEnvelope
from sreda.services.billing import (
    BillingService,
    CONNECT_BASE_CALLBACK,
    STATUS_CALLBACK,
    SUBSCRIPTIONS_CALLBACK,
)
from sreda.services.claim_lookup import ClaimLookupService, is_valid_claim_id
from sreda.services.eds_connect import ConnectSessionError, EDSConnectService
from sreda.services.onboarding import build_connect_eds_message

AGENT_EXECUTE_ACTION_JOB = "agent.execute_action"


class ActionRuntimeError(Exception):
    def __init__(self, code: str, message: str, *, reply_markup: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reply_markup = reply_markup


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

        action: ActionEnvelope | None = None
        context: dict[str, Any] | None = None
        try:
            action = self._load_action(run)
            context = self._load_context(action)
            handler = self._route_action(action.action_type)
            self._policy_guard(action, context)
            replies = handler(action, context)
            await self._persist_and_enqueue_replies(
                job=job,
                run=run,
                action=action,
                context=context,
                replies=replies,
            )
            return "completed"
        except ActionRuntimeError as exc:
            await self._mark_failed(
                job=job,
                run=run,
                action=action,
                context=context,
                error_code=exc.code,
                message=exc.message,
                reply_markup=exc.reply_markup,
            )
            return "failed"
        except Exception:
            await self._mark_failed(
                job=job,
                run=run,
                action=action,
                context=context,
                error_code="runtime_unexpected_error",
                message="Не удалось выполнить действие из-за технической ошибки.",
                reply_markup=None,
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
        billing_summary = BillingService(self.session).get_summary(action.tenant_id)
        eds_monitor_enabled = (
            self.session.query(TenantFeature)
            .filter(
                TenantFeature.tenant_id == action.tenant_id,
                TenantFeature.feature_key == "eds_monitor",
                TenantFeature.enabled.is_(True),
            )
            .one_or_none()
            is not None
        )
        return {
            "tenant_id": action.tenant_id,
            "workspace_id": action.workspace_id,
            "assistant_id": action.assistant_id,
            "eds_monitor_enabled": eds_monitor_enabled,
            "billing_summary": {
                "base_active": billing_summary.base_active,
                "allowed_count": billing_summary.allowed_count,
                "connected_count": billing_summary.connected_count,
                "free_count": billing_summary.free_count,
            },
        }

    def _route_action(self, action_type: str):
        handler = {
            "help.show": self._execute_help_show,
            "status.show": self._execute_status_show,
            "subscriptions.show": self._execute_subscriptions_show,
            "claim.lookup": self._execute_claim_lookup,
            "subscription.connect_base": self._execute_subscription_connect_base,
            "subscription.add_eds": self._execute_subscription_add_eds,
            "subscription.renew_cycle": self._execute_subscription_renew_cycle,
            "eds.connect.start": self._execute_eds_connect_start,
            "eds.connect.retry": self._execute_eds_connect_retry,
            "eds.slot.remove_free": self._execute_eds_slot_remove_free,
            "eds.slot.restore_free": self._execute_eds_slot_restore_free,
            "eds.account.remove": self._execute_eds_account_remove,
            "eds.account.restore": self._execute_eds_account_restore,
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

        summary = context["billing_summary"]
        if action.action_type == "claim.lookup":
            claim_id = str(action.params.get("claim_id") or "").strip()
            if not claim_id:
                raise ActionRuntimeError(
                    "claim_id_missing",
                    "Используй команду в формате:\n\n/claim <номер_заявки>",
                    reply_markup=_status_subscriptions_markup(),
                )
            if not is_valid_claim_id(claim_id):
                raise ActionRuntimeError(
                    "claim_id_invalid",
                    "Номер заявки должен содержать только буквы, цифры, '-' или '_'.",
                    reply_markup=_status_subscriptions_markup(),
                )
            if not context.get("eds_monitor_enabled"):
                raise ActionRuntimeError(
                    "eds_monitor_disabled",
                    "Поиск по заявкам станет доступен после подключения EDS.",
                    reply_markup=_subscriptions_markup(),
                )
            return

        if action.action_type == "subscription.add_eds" and not summary["base_active"]:
            raise ActionRuntimeError(
                "subscription_required",
                "Сначала подключи EDS Monitor, а потом можно будет добавить еще один кабинет.",
                reply_markup=_subscriptions_markup(),
            )
        if action.action_type in {"eds.connect.start", "eds.connect.retry"}:
            if not summary["base_active"]:
                raise ActionRuntimeError(
                    "subscription_required",
                    build_connect_eds_message(
                        base_active=False,
                        connected_count=summary["connected_count"],
                        allowed_count=summary["allowed_count"],
                    ),
                    reply_markup=_connect_reply_markup(False),
                )
            if summary["free_count"] <= 0:
                raise ActionRuntimeError(
                    "limit_exceeded",
                    "Сейчас все оплаченные кабинеты уже заняты.\n\nЕсли нужен еще один кабинет, сначала добавь его в подписках.",
                    reply_markup=_subscriptions_markup(),
                )

    def _execute_help_show(self, action: ActionEnvelope, context: dict[str, Any]) -> list[RuntimeReply]:
        text, reply_markup = BillingService(self.session).build_help_message()
        return [RuntimeReply(text=text, reply_markup=reply_markup)]

    def _execute_status_show(self, action: ActionEnvelope, context: dict[str, Any]) -> list[RuntimeReply]:
        text, reply_markup = BillingService(self.session).build_status_message(action.tenant_id)
        return [RuntimeReply(text=text, reply_markup=reply_markup)]

    def _execute_subscriptions_show(self, action: ActionEnvelope, context: dict[str, Any]) -> list[RuntimeReply]:
        text, reply_markup = BillingService(self.session).build_subscriptions_message(action.tenant_id)
        return [RuntimeReply(text=text, reply_markup=reply_markup)]

    def _execute_claim_lookup(self, action: ActionEnvelope, context: dict[str, Any]) -> list[RuntimeReply]:
        claim_id = str(action.params.get("claim_id") or "").strip()
        service = ClaimLookupService(self.session)
        result = service.lookup_local_claim(action.tenant_id, claim_id)
        if result is None:
            return [
                RuntimeReply(
                    text=(
                        f"Заявка #{claim_id} пока не найдена в локальном состоянии Среды.\n\n"
                        "Если она появилась недавно, попробуй еще раз позже."
                    ),
                    reply_markup=_status_subscriptions_markup(),
                )
            ]
        return [
            RuntimeReply(
                text=service.build_claim_reply(result),
                reply_markup=_status_subscriptions_markup(),
            )
        ]

    def _execute_subscription_connect_base(
        self,
        action: ActionEnvelope,
        context: dict[str, Any],
    ) -> list[RuntimeReply]:
        result = BillingService(self.session).start_base_subscription(action.tenant_id)
        return [RuntimeReply(text=result.message_text, reply_markup=result.reply_markup)]

    def _execute_subscription_add_eds(
        self,
        action: ActionEnvelope,
        context: dict[str, Any],
    ) -> list[RuntimeReply]:
        result = BillingService(self.session).add_extra_eds_account(action.tenant_id)
        replies = [RuntimeReply(text=result.message_text, reply_markup=result.reply_markup)]
        replies.extend(self._build_connect_replies(action, slot_type="extra"))
        return replies

    def _execute_subscription_renew_cycle(
        self,
        action: ActionEnvelope,
        context: dict[str, Any],
    ) -> list[RuntimeReply]:
        result = BillingService(self.session).renew_cycle(action.tenant_id)
        return [RuntimeReply(text=result.message_text, reply_markup=result.reply_markup)]

    def _execute_eds_connect_start(
        self,
        action: ActionEnvelope,
        context: dict[str, Any],
    ) -> list[RuntimeReply]:
        slot_type = str(action.params.get("slot_type") or "available_slot")
        resolved_slot_type = self._resolve_slot_type(action.tenant_id, slot_type)
        return self._build_connect_replies(action, slot_type=resolved_slot_type)

    def _execute_eds_connect_retry(
        self,
        action: ActionEnvelope,
        context: dict[str, Any],
    ) -> list[RuntimeReply]:
        slot_type = str(action.params.get("slot_type") or "")
        return self._build_connect_replies(action, slot_type=slot_type)

    def _execute_eds_slot_remove_free(
        self,
        action: ActionEnvelope,
        context: dict[str, Any],
    ) -> list[RuntimeReply]:
        result = BillingService(self.session).remove_extra_account_at_period_end(action.tenant_id)
        return [RuntimeReply(text=result.message_text, reply_markup=result.reply_markup)]

    def _execute_eds_slot_restore_free(
        self,
        action: ActionEnvelope,
        context: dict[str, Any],
    ) -> list[RuntimeReply]:
        result = BillingService(self.session).restore_extra_account_slot(action.tenant_id)
        return [RuntimeReply(text=result.message_text, reply_markup=result.reply_markup)]

    def _execute_eds_account_remove(
        self,
        action: ActionEnvelope,
        context: dict[str, Any],
    ) -> list[RuntimeReply]:
        tenant_eds_account_id = str(action.params.get("tenant_eds_account_id") or "").strip()
        if not tenant_eds_account_id:
            raise ActionRuntimeError(
                "tenant_eds_account_missing",
                "Не удалось определить кабинет для отключения.",
                reply_markup=_subscriptions_markup(),
            )
        result = BillingService(self.session).schedule_connected_eds_account_cancel(
            action.tenant_id,
            tenant_eds_account_id,
        )
        return [RuntimeReply(text=result.message_text, reply_markup=result.reply_markup)]

    def _execute_eds_account_restore(
        self,
        action: ActionEnvelope,
        context: dict[str, Any],
    ) -> list[RuntimeReply]:
        tenant_eds_account_id = str(action.params.get("tenant_eds_account_id") or "").strip()
        if not tenant_eds_account_id:
            raise ActionRuntimeError(
                "tenant_eds_account_missing",
                "Не удалось определить кабинет для возврата.",
                reply_markup=_subscriptions_markup(),
            )
        result = BillingService(self.session).restore_connected_eds_account_cancel(
            action.tenant_id,
            tenant_eds_account_id,
        )
        return [RuntimeReply(text=result.message_text, reply_markup=result.reply_markup)]

    def _build_connect_replies(self, action: ActionEnvelope, *, slot_type: str) -> list[RuntimeReply]:
        connect_service = EDSConnectService(self.session, get_settings())
        try:
            link = connect_service.create_connect_link(
                tenant_id=action.tenant_id,
                workspace_id=action.workspace_id,
                user_id=action.user_id,
                slot_type=slot_type,
            )
        except ConnectSessionError as exc:
            raise ActionRuntimeError(exc.code, exc.message, reply_markup=_subscriptions_markup()) from exc

        return [
            RuntimeReply(
                text=(
                    "Сейчас откроется защищенная одноразовая страница для подключения личного кабинета EDS.\n\n"
                    "Логин и пароль передаются по защищенному соединению и сохраняются в системе только в зашифрованном виде.\n\n"
                    "Чтобы ввести данные для подключения, нажмите кнопку ниже."
                ),
                reply_markup={
                    "inline_keyboard": [
                        [_build_connect_open_button(link.url)],
                        [{"text": "Отменить", "callback_data": STATUS_CALLBACK}],
                    ]
                },
            )
        ]

    def _resolve_slot_type(self, tenant_id: str, slot_type: str) -> str:
        if slot_type in {"primary", "extra"}:
            return slot_type
        summary = BillingService(self.session).get_summary(tenant_id)
        return "primary" if not summary.connected_accounts else "extra"

    async def _persist_and_enqueue_replies(
        self,
        *,
        job: Job,
        run: AgentRun,
        action: ActionEnvelope,
        context: dict[str, Any],
        replies: list[RuntimeReply],
    ) -> None:
        outbox_items: list[OutboxMessage] = []
        for reply in replies:
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
            outbox_items.append(outbox)

        now = _utcnow()
        job.status = "completed"
        run.status = "completed"
        run.context_json = json.dumps(context, ensure_ascii=False)
        run.result_json = json.dumps(
            {
                "outbox_message_ids": [item.id for item in outbox_items],
                "outbox_statuses": [item.status for item in outbox_items],
                "reply_count": len(outbox_items),
            },
            ensure_ascii=False,
        )
        run.finished_at = now
        self.session.commit()

    async def _mark_failed(
        self,
        *,
        job: Job,
        run: AgentRun,
        action: ActionEnvelope | None,
        context: dict[str, Any] | None,
        error_code: str,
        message: str,
        reply_markup: dict | None,
    ) -> None:
        outbox_ids: list[str] = []
        outbox_statuses: list[str] = []
        if action is not None:
            outbox = OutboxMessage(
                id=f"out_{uuid4().hex[:24]}",
                tenant_id=run.tenant_id,
                workspace_id=run.workspace_id,
                channel_type="telegram",
                status="pending",
                payload_json=json.dumps(
                    {
                        "chat_id": action.external_chat_id,
                        "text": message,
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
                        text=message,
                        reply_markup=reply_markup,
                    )
                    outbox.status = "sent"
                except TelegramDeliveryError:
                    outbox.status = "pending"
            outbox_ids.append(outbox.id)
            outbox_statuses.append(outbox.status)

        now = _utcnow()
        job.status = "failed"
        run.status = "failed"
        if context is not None:
            run.context_json = json.dumps(context, ensure_ascii=False)
        run.result_json = json.dumps(
            {
                "outbox_message_ids": outbox_ids,
                "outbox_statuses": outbox_statuses,
                "reply_count": len(outbox_ids),
            },
            ensure_ascii=False,
        )
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


def _subscriptions_markup() -> dict:
    return {"inline_keyboard": [[{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}]]}


def _status_subscriptions_markup() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
            [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
        ]
    }


def _connect_reply_markup(base_active: bool) -> dict:
    if base_active:
        return _status_subscriptions_markup()
    return {
        "inline_keyboard": [
            [{"text": "Подключить EDS Monitor", "callback_data": CONNECT_BASE_CALLBACK}],
            [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
        ]
    }


def _build_connect_open_button(url: str) -> dict:
    if url.startswith("https://"):
        return {"text": "Ввести логин и пароль от EDS", "web_app": {"url": url}}
    return {"text": "Ввести логин и пароль от EDS", "url": url}


def _utcnow() -> datetime:
    return datetime.now(UTC)
