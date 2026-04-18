"""Skill platform contracts (Phase 0).

Source-of-truth Python models for the skill platform contract per spec 47:
manifest, lifecycle/health statuses, run/attempt statuses, event severity,
retry/retention profiles. These models are Pydantic so they can be easily
validated at registration time and optionally emitted as JSON Schema.

The platform SQLAlchemy tables in ``sreda.db.models.skill_platform`` store
the string values of these enums directly, so if you add a new lifecycle
status here, existing rows keep working — but any code that switches on
``lifecycle_status`` must grow a new branch too.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class SkillLifecycleStatus(str, Enum):
    draft = "draft"
    active = "active"
    paused = "paused"
    degraded = "degraded"
    error = "error"
    disabled = "disabled"


class SkillHealthStatus(str, Enum):
    healthy = "healthy"
    degraded = "degraded"
    unhealthy = "unhealthy"


class SkillRunStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    retry_scheduled = "retry_scheduled"
    cancelled = "cancelled"


class SkillAttemptStatus(str, Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class SkillEventSeverity(str, Enum):
    debug = "debug"
    info = "info"
    warn = "warn"
    error = "error"


class SkillIssueClass(str, Enum):
    config = "config"
    integration = "integration"
    ai = "ai"
    data = "data"
    delivery = "delivery"
    internal = "internal"


class SkillRetryDecision(str, Enum):
    no_retry = "no_retry"
    retry_platform = "retry_platform"
    retry_skill = "retry_skill"
    manual_intervention_required = "manual_intervention_required"


class SkillAIExecutionStatus(str, Enum):
    started = "started"
    succeeded = "succeeded"
    failed = "failed"
    validation_failed = "validation_failed"


class SkillTriggerType(str, Enum):
    manual = "manual"
    schedule = "schedule"
    inbound_event = "inbound_event"
    webhook = "webhook"
    system = "system"


class SkillRetryProfile(BaseModel):
    """Default retry policy for a skill; can be overridden per-tenant later."""

    max_attempts: int = 3
    initial_delay_seconds: int = 60
    backoff_multiplier: float = 2.0
    max_delay_seconds: int = 3600
    jitter: bool = True
    retry_on_issue_classes: list[str] = Field(
        default_factory=lambda: [SkillIssueClass.integration.value, SkillIssueClass.internal.value]
    )
    non_retryable_error_codes: list[str] = Field(default_factory=list)


class SkillRetentionProfile(BaseModel):
    """Per-skill retention windows (in days) for platform tables."""

    runs_days: int = 90
    attempts_days: int = 90
    events_debug_info_days: int = 30
    events_warn_error_days: int = 90
    ai_executions_days: int = 30


class SkillManifestBase(BaseModel):
    """Declarative manifest that every skill exposes via ``get_manifest()``.

    Treat this as immutable per-release metadata: changes to the manifest
    should bump ``manifest_version`` / ``config_schema_version`` /
    ``ai_schema_version`` as appropriate so we can detect breaking config
    changes when the skill package is upgraded.
    """

    feature_key: str
    title: str
    description: str
    manifest_version: int = 1
    config_schema_version: int = 1
    ai_schema_version: int = 1
    default_status: SkillLifecycleStatus = SkillLifecycleStatus.draft
    supports_ai: bool = False
    supports_runtime: bool = False
    supports_user_surfaces: bool = False
    supports_per_tenant_config: bool = True
    # Phase 4.5: does this skill provide free-form chat with the user?
    # Dispatcher routes ``conversation.chat`` actions to the first
    # subscribed skill with ``provides_chat=True``. EDS-monitor style
    # skills (no chat) leave this False.
    provides_chat: bool = False
    # Voice transcription is a shared capability, not a standalone
    # subscription. Any agent that wants incoming voice messages auto-
    # transcribed to text sets this to ``True``. Voice gate in
    # ``services.telegram_bot._maybe_transcribe_voice`` checks whether
    # any subscribed agent has it. Leaving False = voice messages get
    # a "not supported" reply.
    includes_voice: bool = False
    # Optional default for ``SubscriptionPlan.credits_monthly_quota``
    # when seeding new plans for this feature. Doesn't affect runtime
    # enforcement — the plan row is the authoritative source.
    default_credits_monthly_quota: int | None = None
    # Phase 4 LLM-classifier hook: natural-language prompt instructing
    # the classifier how to score this skill's inbound events. Used by
    # the future ``RelevanceClassifierWorker`` when a skill ingests
    # events with ``relevance_score=None``. Left empty by skills that
    # always score their own events via domain rules.
    relevance_prompt: str | None = None
    required_secrets: list[str] = Field(default_factory=list)
    default_retry_profile: SkillRetryProfile = Field(default_factory=SkillRetryProfile)
    default_retention_profile: SkillRetentionProfile = Field(
        default_factory=SkillRetentionProfile
    )


class SkillConfigBase(BaseModel):
    """Base for non-secret config payloads. Concrete skills subclass this."""

    feature_key: str
    config_schema_version: int = 1


class SkillTenantConfigBase(SkillConfigBase):
    tenant_id: str


class SkillSecretConfigBase(BaseModel):
    """Base for skill secrets; stored via ``secure_records``, never in config_json."""

    feature_key: str
    tenant_id: str
