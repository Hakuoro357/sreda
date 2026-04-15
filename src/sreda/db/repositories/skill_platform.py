"""Repository for the skill platform tables (spec 48).

Thin layer over SQLAlchemy: each method performs a single logical mutation
or query and flushes (never commits — the caller owns the transaction).
This matches the pattern of existing repositories like ``tenant_features``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from sreda.db.models.skill_platform import (
    SkillAIExecution,
    SkillEvent,
    SkillRun,
    SkillRunAttempt,
    TenantSkillConfig,
    TenantSkillState,
)
from sreda.features.skill_contracts import (
    SkillAttemptStatus,
    SkillEventSeverity,
    SkillIssueClass,
    SkillLifecycleStatus,
    SkillManifestBase,
    SkillRunStatus,
    SkillTriggerType,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:24]}"


class SkillPlatformRepository:
    """Platform-level CRUD for skill lifecycle, config, runs, attempts, events."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ state

    def get_tenant_skill_state(
        self, tenant_id: str, feature_key: str
    ) -> TenantSkillState | None:
        return (
            self.session.query(TenantSkillState)
            .filter_by(tenant_id=tenant_id, feature_key=feature_key)
            .one_or_none()
        )

    def get_or_create_tenant_skill_state(
        self,
        tenant_id: str,
        feature_key: str,
        *,
        default_status: SkillLifecycleStatus | str = SkillLifecycleStatus.draft,
    ) -> TenantSkillState:
        existing = self.get_tenant_skill_state(tenant_id, feature_key)
        if existing is not None:
            return existing

        status = (
            default_status.value
            if isinstance(default_status, SkillLifecycleStatus)
            else default_status
        )
        now = _utcnow()
        state = TenantSkillState(
            id=_id("tss"),
            tenant_id=tenant_id,
            feature_key=feature_key,
            lifecycle_status=status,
            health_status="healthy",
            last_health_changed_at=now,
            created_at=now,
            updated_at=now,
        )
        self.session.add(state)
        self.session.flush()
        return state

    def set_tenant_skill_status(
        self,
        tenant_id: str,
        feature_key: str,
        lifecycle_status: SkillLifecycleStatus | str,
        *,
        reason_code: str | None = None,
        reason_message_sanitized: str | None = None,
    ) -> TenantSkillState:
        state = self.get_or_create_tenant_skill_state(tenant_id, feature_key)
        state.lifecycle_status = (
            lifecycle_status.value
            if isinstance(lifecycle_status, SkillLifecycleStatus)
            else lifecycle_status
        )
        state.status_reason_code = reason_code
        state.status_reason_message_sanitized = reason_message_sanitized
        state.updated_at = _utcnow()
        self.session.flush()
        return state

    # ----------------------------------------------------------------- config

    def upsert_tenant_skill_config(
        self,
        tenant_id: str,
        feature_key: str,
        *,
        config_payload: dict[str, Any],
        config_schema_version: int = 1,
        secure_record_id: str | None = None,
        updated_by_user_id: str | None = None,
    ) -> TenantSkillConfig:
        row = (
            self.session.query(TenantSkillConfig)
            .filter_by(tenant_id=tenant_id, feature_key=feature_key)
            .one_or_none()
        )
        payload_text = json.dumps(config_payload, sort_keys=True, ensure_ascii=False)
        now = _utcnow()
        if row is None:
            row = TenantSkillConfig(
                id=_id("tsc"),
                tenant_id=tenant_id,
                feature_key=feature_key,
                config_schema_version=config_schema_version,
                config_json=payload_text,
                secure_record_id=secure_record_id,
                updated_by_user_id=updated_by_user_id,
                created_at=now,
                updated_at=now,
            )
            self.session.add(row)
        else:
            row.config_schema_version = config_schema_version
            row.config_json = payload_text
            if secure_record_id is not None:
                row.secure_record_id = secure_record_id
            if updated_by_user_id is not None:
                row.updated_by_user_id = updated_by_user_id
            row.updated_at = now
        self.session.flush()
        return row

    # ------------------------------------------------------------------- runs

    def create_skill_run(
        self,
        *,
        tenant_id: str,
        feature_key: str,
        run_key: str,
        trigger_type: SkillTriggerType | str = SkillTriggerType.manual,
        trigger_ref: str | None = None,
        workspace_id: str | None = None,
        assistant_id: str | None = None,
        input_json: str | None = None,
        max_attempts: int = 3,
    ) -> SkillRun:
        now = _utcnow()
        run = SkillRun(
            id=_id("skr"),
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            assistant_id=assistant_id,
            feature_key=feature_key,
            trigger_type=(
                trigger_type.value if isinstance(trigger_type, SkillTriggerType) else trigger_type
            ),
            trigger_ref=trigger_ref,
            run_key=run_key,
            status=SkillRunStatus.queued.value,
            input_json=input_json,
            current_attempt=0,
            max_attempts=max_attempts,
            created_at=now,
        )
        self.session.add(run)
        self.session.flush()
        return run

    def mark_skill_run_running(self, run_id: str) -> None:
        run = self.session.get(SkillRun, run_id)
        if run is None:
            return
        now = _utcnow()
        run.status = SkillRunStatus.running.value
        if run.started_at is None:
            run.started_at = now

    def complete_skill_run(self, run_id: str, *, output_json: str | None = None) -> None:
        run = self.session.get(SkillRun, run_id)
        if run is None:
            return
        now = _utcnow()
        run.status = SkillRunStatus.succeeded.value
        run.finished_at = now
        run.output_json = output_json
        state = self.get_tenant_skill_state(run.tenant_id, run.feature_key)
        if state is not None:
            state.last_run_id = run.id
            state.last_successful_run_at = now
            state.updated_at = now

    def fail_skill_run(
        self,
        run_id: str,
        *,
        error_code: str,
        error_message_sanitized: str | None = None,
    ) -> None:
        run = self.session.get(SkillRun, run_id)
        if run is None:
            return
        now = _utcnow()
        run.status = SkillRunStatus.failed.value
        run.finished_at = now
        run.error_code = error_code
        run.error_message_sanitized = error_message_sanitized
        state = self.get_tenant_skill_state(run.tenant_id, run.feature_key)
        if state is not None:
            state.last_run_id = run.id
            state.last_failed_run_at = now
            state.updated_at = now

    # ---------------------------------------------------------------- attempts

    def create_skill_run_attempt(
        self,
        *,
        run_id: str,
        tenant_id: str,
        feature_key: str,
        attempt_number: int,
        workspace_id: str | None = None,
        assistant_id: str | None = None,
        job_id: str | None = None,
        worker_id: str | None = None,
    ) -> SkillRunAttempt:
        now = _utcnow()
        attempt = SkillRunAttempt(
            id=_id("skra"),
            run_id=run_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            assistant_id=assistant_id,
            feature_key=feature_key,
            attempt_number=attempt_number,
            job_id=job_id,
            status=SkillAttemptStatus.running.value,
            worker_id=worker_id,
            created_at=now,
            started_at=now,
        )
        self.session.add(attempt)
        # keep run.current_attempt in sync — spec invariant
        run = self.session.get(SkillRun, run_id)
        if run is not None:
            run.current_attempt = attempt_number
        self.session.flush()
        return attempt

    def complete_skill_run_attempt(
        self,
        attempt_id: str,
        *,
        status: SkillAttemptStatus | str = SkillAttemptStatus.succeeded,
        error_class: str | None = None,
        error_code: str | None = None,
        error_message_sanitized: str | None = None,
        retry_decision: str | None = None,
    ) -> None:
        attempt = self.session.get(SkillRunAttempt, attempt_id)
        if attempt is None:
            return
        now = _utcnow()
        attempt.status = status.value if isinstance(status, SkillAttemptStatus) else status
        attempt.finished_at = now
        attempt.error_class = error_class
        attempt.error_code = error_code
        attempt.error_message_sanitized = error_message_sanitized
        attempt.retry_decision = retry_decision
        if attempt.started_at is not None:
            started = attempt.started_at
            if started.tzinfo is None:
                # SQLite strips tzinfo on roundtrip — assume UTC.
                started = started.replace(tzinfo=timezone.utc)
            delta = (now - started).total_seconds() * 1000
            attempt.latency_ms = int(max(0, delta))

    # ------------------------------------------------------------------ events

    def append_skill_event(
        self,
        *,
        tenant_id: str,
        feature_key: str,
        severity: SkillEventSeverity | str,
        event_type: str,
        message: str,
        issue_class: SkillIssueClass | str | None = None,
        run_id: str | None = None,
        attempt_id: str | None = None,
        workspace_id: str | None = None,
        assistant_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> SkillEvent:
        evt = SkillEvent(
            id=_id("ske"),
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            assistant_id=assistant_id,
            feature_key=feature_key,
            run_id=run_id,
            attempt_id=attempt_id,
            severity=(
                severity.value if isinstance(severity, SkillEventSeverity) else severity
            ),
            issue_class=(
                issue_class.value
                if isinstance(issue_class, SkillIssueClass)
                else issue_class
            ),
            event_type=event_type,
            message=message,
            details_json=(
                json.dumps(details, sort_keys=True, ensure_ascii=False)
                if details is not None
                else None
            ),
            created_at=_utcnow(),
        )
        self.session.add(evt)
        self.session.flush()
        return evt

    # -------------------------------------------------------------- ai records

    def record_skill_ai_execution(
        self,
        *,
        run_id: str,
        tenant_id: str,
        feature_key: str,
        task_type: str | None = None,
        provider_key: str | None = None,
        model: str | None = None,
        ai_schema_version: int = 1,
        attempt_id: str | None = None,
        status: str = "started",
        structured_output_json: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> SkillAIExecution:
        now = _utcnow()
        row = SkillAIExecution(
            id=_id("skai"),
            run_id=run_id,
            attempt_id=attempt_id,
            tenant_id=tenant_id,
            feature_key=feature_key,
            task_type=task_type,
            provider_key=provider_key,
            model=model,
            ai_schema_version=ai_schema_version,
            status=status,
            structured_output_json=structured_output_json,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            created_at=now,
            started_at=now,
        )
        self.session.add(row)
        self.session.flush()
        return row

    # ---------------------------------------------------------- manifest helper

    def ensure_manifest_state(
        self,
        tenant_id: str,
        manifest: SkillManifestBase,
    ) -> TenantSkillState:
        """Lazy initialization hook: make sure there's a platform state row
        for (tenant, manifest.feature_key), using the manifest's default
        lifecycle status for first-time creation."""
        return self.get_or_create_tenant_skill_state(
            tenant_id,
            manifest.feature_key,
            default_status=manifest.default_status,
        )
