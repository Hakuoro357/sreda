from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.orm import Session

from sreda.db.models.eds_monitor import EDSAccount
from sreda.db.models.connect import ConnectSession, TenantEDSAccount
from sreda.db.models.core import Job, SecureRecord, User
from sreda.integrations.telegram.client import TelegramClient
from sreda.services.billing import BillingService, STATUS_CALLBACK, SUBSCRIPTIONS_CALLBACK
from sreda.services.secure_storage import load_secure_json, store_secure_json

RETRY_CONNECT_PRIMARY_CALLBACK = "eds:retry_connect:primary"
RETRY_CONNECT_EXTRA_CALLBACK = "eds:retry_connect:extra"

AUTH_FAILURE_CODES = {"verification_auth_failed"}
RETRY_LIMITS = {
    "verification_temporary_failed": 2,
    "verification_unknown_failed": 1,
}


@dataclass(slots=True)
class VerificationResult:
    login_masked: str


class VerificationAdapter(Protocol):
    async def verify_account(
        self,
        *,
        account_key: str,
        login: str,
        password: str,
    ) -> VerificationResult: ...


class VerificationError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class DefaultEDSVerificationAdapter:
    async def verify_account(
        self,
        *,
        account_key: str,
        login: str,
        password: str,
    ) -> VerificationResult:
        try:
            from sreda_feature_eds_monitor.integrations.client import EDSMonitorClient
        except ModuleNotFoundError as exc:
            raise VerificationError(
                "verification_unknown_failed",
                "EDS verification adapter is not installed.",
                retryable=False,
            ) from exc

        client = EDSMonitorClient()
        try:
            await client.login(
                account_key,
                login=login,
                password=password,
                headed=False,
                timeout_seconds=120,
            )
            await client.activate_operator_role(account_key, login=login)
            await client.fetch_claims(account_key, max_pages=1, login=login)
        except Exception as exc:  # pragma: no cover - integration branch
            message = str(exc).strip() or "EDS verification failed."
            lowered = message.lower()
            if "401" in lowered or "403" in lowered or "password" in lowered or "логин" in lowered:
                raise VerificationError(
                    "verification_auth_failed",
                    "Не удалось подключить кабинет. Проверь логин и пароль.",
                    retryable=False,
                ) from exc
            if "timeout" in lowered or "timed out" in lowered or "tempor" in lowered:
                raise VerificationError(
                    "verification_temporary_failed",
                    "Временная ошибка подключения. Попробуй еще раз позже.",
                    retryable=True,
                ) from exc
            raise VerificationError(
                "verification_unknown_failed",
                "Не удалось завершить подключение из-за технической ошибки.",
                retryable=True,
            ) from exc

        return VerificationResult(login_masked=_mask_login(login))


class EDSAccountVerificationService:
    def __init__(
        self,
        session: Session,
        *,
        telegram_client: TelegramClient | None = None,
        adapter: VerificationAdapter | None = None,
    ) -> None:
        self.session = session
        self.telegram_client = telegram_client
        self.adapter = adapter or DefaultEDSVerificationAdapter()

    async def process_pending_jobs(self, *, limit: int = 20) -> int:
        jobs = (
            self.session.query(Job)
            .filter(Job.job_type == "eds.verify_account_connect", Job.status == "pending")
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
        if job.job_type != "eds.verify_account_connect":
            return "skipped"
        if job.status == "completed":
            return "completed"
        if job.status not in {"pending", "running"}:
            return "skipped"

        connect_session_id = _extract_connect_session_id(job.payload_json)
        if not connect_session_id:
            job.status = "failed"
            self.session.commit()
            return "failed"

        connect_session = self.session.get(ConnectSession, connect_session_id)
        if connect_session is None:
            job.status = "failed"
            self.session.commit()
            return "failed"
        if connect_session.status == "verified":
            job.status = "completed"
            self.session.commit()
            return "completed"
        if connect_session.status in {"failed", "expired"}:
            job.status = "failed"
            self.session.commit()
            return "failed"

        payload_record = self._load_payload_record(connect_session)
        if payload_record is None:
            self._fail_verification(
                job=job,
                connect_session=connect_session,
                tenant_account=self._load_tenant_account(connect_session),
                error_code="secure_store_failed",
                error_message="Не удалось завершить подключение из-за технической ошибки.",
            )
            await self._send_failure_message(connect_session, tenant_account=self._load_tenant_account(connect_session))
            return "failed"

        payload = load_secure_json(payload_record)
        login = str(payload.get("login") or "").strip()
        password = str(payload.get("password") or "").strip()
        if not login or not password:
            self._fail_verification(
                job=job,
                connect_session=connect_session,
                tenant_account=self._load_tenant_account(connect_session),
                error_code="secure_store_failed",
                error_message="Не удалось завершить подключение из-за технической ошибки.",
            )
            await self._send_failure_message(connect_session, tenant_account=self._load_tenant_account(connect_session))
            return "failed"

        tenant_account = self._load_tenant_account(connect_session)
        if tenant_account is None:
            self._fail_verification(
                job=job,
                connect_session=connect_session,
                tenant_account=None,
                error_code="verification_unknown_failed",
                error_message="Не удалось завершить подключение из-за технической ошибки.",
            )
            await self._send_failure_message(connect_session, tenant_account=None)
            return "failed"

        job.status = "running"
        self.session.commit()

        try:
            result = await self.adapter.verify_account(
                account_key=tenant_account.id,
                login=login,
                password=password,
            )
        except VerificationError as exc:
            return await self._handle_verification_error(
                job=job,
                connect_session=connect_session,
                tenant_account=tenant_account,
                error=exc,
            )

        credential_record = store_secure_json(
            self.session,
            record_type="eds_account_credentials",
            record_key=tenant_account.id,
            value={
                "login": login,
                "password": password,
                "tenant_eds_account_id": tenant_account.id,
                "rotated_at": _utcnow().isoformat(),
            },
            tenant_id=tenant_account.tenant_id,
            workspace_id=tenant_account.workspace_id,
        )
        self.session.flush()
        runtime_account = self._upsert_runtime_eds_account(
            tenant_account=tenant_account,
            login=login,
        )

        now = _utcnow()
        tenant_account.status = "active"
        tenant_account.login_masked = result.login_masked
        tenant_account.secure_record_id = credential_record.id
        tenant_account.last_connect_session_id = connect_session.id
        tenant_account.last_verified_at = now
        tenant_account.last_error_code = None
        tenant_account.last_error_message_sanitized = None
        tenant_account.updated_at = now

        connect_session.status = "verified"
        connect_session.verified_at = now
        connect_session.used_at = connect_session.used_at or now
        connect_session.tenant_eds_account_id = tenant_account.id
        connect_session.error_code = None
        connect_session.error_message_sanitized = None
        connect_session.updated_at = now

        job.status = "completed"
        self.session.commit()
        await self._send_success_message(connect_session, tenant_account)
        return "completed"

    def _load_payload_record(self, connect_session: ConnectSession) -> SecureRecord | None:
        if not connect_session.secure_record_id:
            return None
        record = self.session.get(SecureRecord, connect_session.secure_record_id)
        if record is None or record.record_type != "eds_connect_payload":
            return None
        return record

    def _load_tenant_account(self, connect_session: ConnectSession) -> TenantEDSAccount | None:
        if not connect_session.tenant_eds_account_id:
            return None
        return self.session.get(TenantEDSAccount, connect_session.tenant_eds_account_id)

    def _upsert_runtime_eds_account(
        self,
        *,
        tenant_account: TenantEDSAccount,
        login: str,
    ) -> EDSAccount:
        if not tenant_account.assistant_id:
            raise VerificationError(
                "verification_unknown_failed",
                "Не удалось завершить подключение из-за технической ошибки.",
                retryable=False,
            )
        runtime_account = (
            self.session.query(EDSAccount)
            .filter(EDSAccount.tenant_eds_account_id == tenant_account.id)
            .one_or_none()
        )
        if runtime_account is None:
            runtime_account = EDSAccount(
                id=f"eds_acc_{tenant_account.id}",
                tenant_id=tenant_account.tenant_id,
                workspace_id=tenant_account.workspace_id,
                assistant_id=tenant_account.assistant_id,
                tenant_eds_account_id=tenant_account.id,
                site_key="eds",
                account_key=tenant_account.id,
                label=f"EDS кабинет {tenant_account.account_index}",
                login=login,
            )
            self.session.add(runtime_account)
            self.session.flush()
            return runtime_account

        runtime_account.tenant_id = tenant_account.tenant_id
        runtime_account.workspace_id = tenant_account.workspace_id
        runtime_account.assistant_id = tenant_account.assistant_id or runtime_account.assistant_id
        runtime_account.tenant_eds_account_id = tenant_account.id
        runtime_account.site_key = "eds"
        runtime_account.account_key = tenant_account.id
        runtime_account.label = f"EDS кабинет {tenant_account.account_index}"
        runtime_account.login = login
        self.session.flush()
        return runtime_account

    async def _handle_verification_error(
        self,
        *,
        job: Job,
        connect_session: ConnectSession,
        tenant_account: TenantEDSAccount,
        error: VerificationError,
    ) -> str:
        attempts = _extract_attempts(job.payload_json) + 1
        max_attempts = RETRY_LIMITS.get(error.code, 0)
        if error.retryable and attempts <= max_attempts:
            job.status = "pending"
            job.payload_json = json.dumps(
                {
                    "connect_session_id": connect_session.id,
                    "attempts": attempts,
                },
                ensure_ascii=False,
            )
            self.session.commit()
            return "retry_scheduled"

        self._fail_verification(
            job=job,
            connect_session=connect_session,
            tenant_account=tenant_account,
            error_code=error.code,
            error_message=error.message,
        )
        await self._send_failure_message(connect_session, tenant_account=tenant_account)
        return "failed"

    def _fail_verification(
        self,
        *,
        job: Job,
        connect_session: ConnectSession,
        tenant_account: TenantEDSAccount | None,
        error_code: str,
        error_message: str,
    ) -> None:
        now = _utcnow()
        connect_session.status = "failed"
        connect_session.failed_at = now
        connect_session.error_code = error_code
        connect_session.error_message_sanitized = error_message
        connect_session.updated_at = now

        if tenant_account is not None:
            tenant_account.status = "auth_failed" if error_code in AUTH_FAILURE_CODES else "pending_verification"
            tenant_account.last_error_code = error_code
            tenant_account.last_error_message_sanitized = error_message
            tenant_account.updated_at = now

        job.status = "failed"
        self.session.commit()

    async def _send_success_message(
        self,
        connect_session: ConnectSession,
        tenant_account: TenantEDSAccount,
    ) -> None:
        chat_id = self._get_recipient_chat_id(connect_session.tenant_id)
        if self.telegram_client is None or chat_id is None:
            return

        summary = BillingService(self.session).get_summary(connect_session.tenant_id)
        connected_count = summary.connected_count
        allowed_count = summary.allowed_count
        if tenant_account.account_role == "primary":
            text = (
                "Кабинет EDS подключен.\n\n"
                "Теперь мониторинг активен.\n"
                f"Подключено кабинетов: {connected_count} из {allowed_count}"
            )
        else:
            text = (
                "Дополнительный кабинет EDS подключен.\n\n"
                f"Подключено кабинетов: {connected_count} из {allowed_count}"
            )
        await self.telegram_client.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup={
                "inline_keyboard": [
                    [{"text": "Последние события", "callback_data": "events:latest"}],
                    [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                    [{"text": "Подписки", "callback_data": SUBSCRIPTIONS_CALLBACK}],
                ]
            },
        )

    async def _send_failure_message(
        self,
        connect_session: ConnectSession,
        *,
        tenant_account: TenantEDSAccount | None,
    ) -> None:
        chat_id = self._get_recipient_chat_id(connect_session.tenant_id)
        if self.telegram_client is None or chat_id is None:
            return

        text = connect_session.error_message_sanitized or "Не удалось завершить подключение из-за технической ошибки."
        retry_callback = (
            RETRY_CONNECT_PRIMARY_CALLBACK
            if (tenant_account is None or tenant_account.account_role == "primary")
            else RETRY_CONNECT_EXTRA_CALLBACK
        )
        await self.telegram_client.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup={
                "inline_keyboard": [
                    [{"text": "Повторить подключение", "callback_data": retry_callback}],
                    [{"text": "Мой статус", "callback_data": STATUS_CALLBACK}],
                ]
            },
        )

    def _get_recipient_chat_id(self, tenant_id: str) -> str | None:
        user = (
            self.session.query(User)
            .filter(User.tenant_id == tenant_id, User.telegram_account_id.is_not(None))
            .order_by(User.id.asc())
            .first()
        )
        return user.telegram_account_id if user and user.telegram_account_id else None

def _extract_connect_session_id(payload_json: str) -> str | None:
    try:
        payload = json.loads(payload_json or "{}")
    except json.JSONDecodeError:
        return None
    value = payload.get("connect_session_id")
    return str(value) if value else None


def _extract_attempts(payload_json: str) -> int:
    try:
        payload = json.loads(payload_json or "{}")
    except json.JSONDecodeError:
        return 0
    value = payload.get("attempts")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _mask_login(login: str) -> str:
    if len(login) <= 4:
        return "*" * len(login)
    return f"{login[:4]}***{login[-3:]}"


def _utcnow() -> datetime:
    return datetime.now(UTC)
