from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.config.settings import Settings
from sreda.db.models.connect import ConnectSession, TenantEDSAccount
from sreda.db.models.core import Assistant, Job
from sreda.services.billing import BillingService
from sreda.services.secure_storage import store_secure_json


SESSION_TTL_MINUTES = 15


class ConnectSessionError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(slots=True)
class ConnectLinkResult:
    session_id: str
    raw_token: str
    url: str
    slot_type: str
    expires_at: datetime


@dataclass(slots=True)
class ConnectSubmitResult:
    session_id: str
    tenant_eds_account_id: str
    job_id: str


class EDSConnectService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def create_connect_link(
        self,
        *,
        tenant_id: str,
        workspace_id: str | None,
        user_id: str | None,
        slot_type: str,
    ) -> ConnectLinkResult:
        if slot_type not in {"primary", "extra"}:
            raise ConnectSessionError("invalid_slot", "Invalid account slot type.", status_code=400)

        base_url = (self.settings.connect_public_base_url or "").strip().rstrip("/")
        if not base_url:
            raise ConnectSessionError(
                "connect_not_configured",
                "Public connect URL is not configured.",
                status_code=500,
            )

        summary = BillingService(self.session).get_summary(tenant_id)
        if not summary.base_active:
            raise ConnectSessionError(
                "subscription_required",
                "Active EDS Monitor subscription is required.",
                status_code=409,
            )
        if summary.connected_count >= summary.allowed_count:
            raise ConnectSessionError(
                "limit_exceeded",
                "No free EDS account slots are available.",
                status_code=409,
            )

        if slot_type == "primary" and self._has_primary_account(tenant_id):
            slot_type = "extra"

        raw_token = secrets.token_urlsafe(24)
        token_hash = _hash_token(raw_token)
        expires_at = _utcnow() + timedelta(minutes=SESSION_TTL_MINUTES)
        connect_session = ConnectSession(
            id=f"cs_{uuid4().hex[:24]}",
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            user_id=user_id,
            session_type="eds_connect",
            account_slot_type=slot_type,
            one_time_token_hash=token_hash,
            status="created",
            expires_at=expires_at,
        )
        self.session.add(connect_session)
        self.session.commit()

        return ConnectLinkResult(
            session_id=connect_session.id,
            raw_token=raw_token,
            url=f"{base_url}/connect/eds/{raw_token}",
            slot_type=slot_type,
            expires_at=expires_at,
        )

    def open_form(self, raw_token: str) -> ConnectSession:
        connect_session = self._require_valid_session(raw_token)
        if connect_session.opened_at is None:
            connect_session.opened_at = _utcnow()
            connect_session.status = "opened"
            connect_session.updated_at = _utcnow()
            self.session.commit()
        return connect_session

    def submit_form(self, raw_token: str, *, login: str, password: str) -> ConnectSubmitResult:
        normalized_login = login.strip()
        normalized_password = password.strip()
        if not normalized_login or not normalized_password:
            raise ConnectSessionError("form_invalid", "Заполни логин и пароль.", status_code=400)

        connect_session = self._require_valid_session(raw_token)
        if connect_session.used_at is not None:
            raise ConnectSessionError("session_used", "Эта ссылка уже использована.", status_code=410)

        billing_summary = BillingService(self.session).get_summary(connect_session.tenant_id)
        if billing_summary.connected_count >= billing_summary.allowed_count:
            raise ConnectSessionError(
                "limit_exceeded",
                "Нет свободного лимита кабинетов.",
                status_code=409,
            )

        secure_record = store_secure_json(
            self.session,
            record_type="eds_connect_payload",
            record_key=connect_session.id,
            value={
                "login": normalized_login,
                "password": normalized_password,
                "submitted_at": _utcnow().isoformat(),
                "connect_session_id": connect_session.id,
            },
            tenant_id=connect_session.tenant_id,
            workspace_id=connect_session.workspace_id,
        )

        tenant_eds_account = TenantEDSAccount(
            id=f"teds_{uuid4().hex[:24]}",
            tenant_id=connect_session.tenant_id,
            workspace_id=connect_session.workspace_id or self._get_workspace_id(connect_session.tenant_id),
            assistant_id=self._get_assistant_id(connect_session.tenant_id),
            account_index=str(self._next_account_index(connect_session.tenant_id)),
            account_role=connect_session.account_slot_type,
            status="pending_verification",
            login_masked=_mask_login(normalized_login),
            secure_record_id=secure_record.id,
            last_connect_session_id=connect_session.id,
        )
        self.session.add(tenant_eds_account)
        self.session.flush()

        job = Job(
            id=f"job_{uuid4().hex[:24]}",
            tenant_id=connect_session.tenant_id,
            workspace_id=tenant_eds_account.workspace_id,
            job_type="eds.verify_account_connect",
            status="pending",
            payload_json=json.dumps(
                {"connect_session_id": connect_session.id},
                ensure_ascii=False,
            ),
        )
        self.session.add(job)

        now = _utcnow()
        connect_session.secure_record_id = secure_record.id
        connect_session.tenant_eds_account_id = tenant_eds_account.id
        connect_session.submitted_at = now
        connect_session.used_at = now
        connect_session.status = "submitted"
        connect_session.updated_at = now
        self.session.commit()

        return ConnectSubmitResult(
            session_id=connect_session.id,
            tenant_eds_account_id=tenant_eds_account.id,
            job_id=job.id,
        )

    def _require_valid_session(self, raw_token: str) -> ConnectSession:
        token_hash = _hash_token(raw_token)
        connect_session = (
            self.session.query(ConnectSession)
            .filter(ConnectSession.one_time_token_hash == token_hash)
            .one_or_none()
        )
        if connect_session is None:
            raise ConnectSessionError(
                "token_not_found",
                "Ссылка недействительна. Запроси новую в Telegram.",
                status_code=404,
            )
        if connect_session.expires_at and _coerce_utc(connect_session.expires_at) <= _utcnow():
            connect_session.status = "expired"
            connect_session.updated_at = _utcnow()
            self.session.commit()
            raise ConnectSessionError(
                "session_expired",
                "Срок действия ссылки истек. Запроси новую в Telegram.",
                status_code=410,
            )
        if connect_session.used_at is not None:
            raise ConnectSessionError(
                "session_used",
                "Эта ссылка уже использована.",
                status_code=410,
            )
        if connect_session.session_type != "eds_connect":
            raise ConnectSessionError("token_not_found", "Ссылка недействительна.", status_code=404)
        return connect_session

    def _next_account_index(self, tenant_id: str) -> int:
        values = (
            self.session.query(TenantEDSAccount.account_index)
            .filter(TenantEDSAccount.tenant_id == tenant_id)
            .all()
        )
        numeric_values = []
        for (value,) in values:
            try:
                numeric_values.append(int(value))
            except (TypeError, ValueError):
                continue
        return max(numeric_values, default=0) + 1

    def _get_workspace_id(self, tenant_id: str) -> str:
        assistant = (
            self.session.query(Assistant)
            .filter(Assistant.tenant_id == tenant_id)
            .order_by(Assistant.id.asc())
            .first()
        )
        if assistant is not None:
            return assistant.workspace_id
        raise ConnectSessionError("workspace_missing", "Workspace is not configured.", status_code=500)

    def _get_assistant_id(self, tenant_id: str) -> str | None:
        assistant = (
            self.session.query(Assistant)
            .filter(Assistant.tenant_id == tenant_id)
            .order_by(Assistant.id.asc())
            .first()
        )
        return assistant.id if assistant is not None else None

    def _has_primary_account(self, tenant_id: str) -> bool:
        return (
            self.session.query(TenantEDSAccount)
            .filter(
                TenantEDSAccount.tenant_id == tenant_id,
                TenantEDSAccount.account_role == "primary",
                TenantEDSAccount.status.in_(["pending_verification", "active", "auth_failed"]),
            )
            .first()
            is not None
        )


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _mask_login(login: str) -> str:
    if len(login) <= 4:
        return "*" * len(login)
    return f"{login[:4]}***{login[-3:]}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _coerce_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
