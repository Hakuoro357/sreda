"""Skill platform SQLAlchemy models (Phase 0, spec 48).

Tables:
  * ``tenant_skill_states``    — lifecycle/health per (tenant, feature_key)
  * ``tenant_skill_configs``   — non-secret tenant overrides
  * ``skill_runs``             — logical execution unit
  * ``skill_run_attempts``     — per-attempt execution record
  * ``skill_events``           — normalized observability stream
  * ``skill_ai_executions``    — AI call metadata + validated structured output

Design notes:
  * ``run_id`` / ``attempt_id`` / ``job_id`` on dependent tables are stored
    as plain strings (no FK) so retention cleanup can purge parent rows
    without cascading issues and without requiring a fixed deletion order.
  * FKs are kept only where they enforce invariants worth paying for at
    cleanup time (``tenant_id`` → ``tenants.id``, ``skill_run_attempts.run_id``
    → ``skill_runs.id`` because an orphan attempt makes no sense).
  * All statuses are stored as raw strings so we can add new enum values
    without a schema migration (see ``skill_contracts``).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sreda.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TenantSkillState(Base):
    __tablename__ = "tenant_skill_states"
    __table_args__ = (
        UniqueConstraint("tenant_id", "feature_key", name="uq_tenant_skill_states_tenant_feature"),
        Index("ix_tenant_skill_states_lifecycle_status", "lifecycle_status"),
        Index("ix_tenant_skill_states_health_status", "health_status"),
        Index("ix_tenant_skill_states_next_run_at", "next_run_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    feature_key: Mapped[str] = mapped_column(String(64), index=True)
    lifecycle_status: Mapped[str] = mapped_column(String(32), default="draft")
    health_status: Mapped[str] = mapped_column(String(32), default="healthy")
    status_reason_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status_reason_message_sanitized: Mapped[str | None] = mapped_column(Text, nullable=True)
    # soft reference — no FK to avoid circular/order issues during cleanup
    last_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_successful_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failed_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_health_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class TenantSkillConfig(Base):
    __tablename__ = "tenant_skill_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "feature_key", name="uq_tenant_skill_configs_tenant_feature"),
        Index("ix_tenant_skill_configs_schema_version", "config_schema_version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    feature_key: Mapped[str] = mapped_column(String(64), index=True)
    config_schema_version: Mapped[int] = mapped_column(Integer, default=1)
    # Non-secret tenant overrides only. Must NOT contain tokens/passwords/cookies.
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    config_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    secure_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("secure_records.id"), nullable=True, index=True
    )
    updated_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SkillRun(Base):
    __tablename__ = "skill_runs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "feature_key", "run_key", name="uq_skill_runs_tenant_feature_runkey"
        ),
        Index("ix_skill_runs_tenant_created", "tenant_id", "created_at"),
        Index("ix_skill_runs_feature_created", "feature_key", "created_at"),
        Index("ix_skill_runs_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True, index=True
    )
    assistant_id: Mapped[str | None] = mapped_column(
        ForeignKey("assistants.id"), nullable=True, index=True
    )
    feature_key: Mapped[str] = mapped_column(String(64), index=True)
    trigger_type: Mapped[str] = mapped_column(String(32), default="manual")
    trigger_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    run_key: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    input_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_secure_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("secure_records.id"), nullable=True
    )
    output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_secure_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("secure_records.id"), nullable=True
    )
    current_attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message_sanitized: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SkillRunAttempt(Base):
    __tablename__ = "skill_run_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt_number", name="uq_skill_run_attempts_run_attempt"),
        Index("ix_skill_run_attempts_job_id", "job_id"),
        Index("ix_skill_run_attempts_tenant_created", "tenant_id", "created_at"),
        Index("ix_skill_run_attempts_feature_created", "feature_key", "created_at"),
        Index("ix_skill_run_attempts_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("skill_runs.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True
    )
    assistant_id: Mapped[str | None] = mapped_column(
        ForeignKey("assistants.id"), nullable=True
    )
    feature_key: Mapped[str] = mapped_column(String(64), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    # soft ref — jobs table can be purged independently by retention
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message_sanitized: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_decision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SkillEvent(Base):
    __tablename__ = "skill_events"
    __table_args__ = (
        Index("ix_skill_events_tenant_created", "tenant_id", "created_at"),
        Index("ix_skill_events_feature_created", "feature_key", "created_at"),
        Index("ix_skill_events_run_created", "run_id", "created_at"),
        Index("ix_skill_events_attempt_created", "attempt_id", "created_at"),
        Index("ix_skill_events_severity_created", "severity", "created_at"),
        Index("ix_skill_events_issue_class_created", "issue_class", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    workspace_id: Mapped[str | None] = mapped_column(
        ForeignKey("workspaces.id"), nullable=True
    )
    assistant_id: Mapped[str | None] = mapped_column(
        ForeignKey("assistants.id"), nullable=True
    )
    feature_key: Mapped[str] = mapped_column(String(64), index=True)
    # soft refs — cleanup on parent doesn't cascade here
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    issue_class: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    message: Mapped[str] = mapped_column(Text)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )


class SkillAIExecution(Base):
    __tablename__ = "skill_ai_executions"
    __table_args__ = (
        Index("ix_skill_ai_run_created", "run_id", "created_at"),
        Index("ix_skill_ai_attempt_created", "attempt_id", "created_at"),
        Index("ix_skill_ai_tenant_created", "tenant_id", "created_at"),
        Index("ix_skill_ai_feature_created", "feature_key", "created_at"),
        Index("ix_skill_ai_provider_model_created", "provider_key", "model", "created_at"),
        Index("ix_skill_ai_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # soft refs to skill_runs / skill_run_attempts
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    feature_key: Mapped[str] = mapped_column(String(64), index=True)
    task_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ai_schema_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="started", index=True)
    finish_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    validation_status: Mapped[str] = mapped_column(String(32), default="not_checked")
    safety_flags_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_artifact_secure_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("secure_records.id"), nullable=True
    )
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost_rub_micro: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # MiMo credits consumed (Phase 4.5). Computed from model + tokens via
    # ``sreda.services.credit_formula.credits_for`` at write time. Used
    # for per-skill quota enforcement against
    # ``SubscriptionPlan.credits_monthly_quota``.
    credits_consumed: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message_sanitized: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
