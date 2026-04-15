"""Internal stub skill — Phase 0 canary.

Purpose: prove out the skill platform contract (manifest + lifecycle
state + skill_run creation + retention cleanup) without touching any
real domain logic. Used by integration tests and as a reference
implementation for future skills.

Enable via env:
    SREDA_FEATURE_MODULES=sreda.features.stub_skill

The stub registers a single job handler for ``stub_skill.noop`` that
does nothing. Creating a ``Job(job_type='stub_skill.noop', ...)`` and
running the job processor causes the platform to record a
``skill_run`` + ``skill_run_attempt`` around the no-op handler.
"""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy.orm import Session

from sreda.db.models.core import Job
from sreda.features.registry import FeatureRegistry
from sreda.features.skill_contracts import (
    SkillLifecycleStatus,
    SkillManifestBase,
    SkillRetentionProfile,
    SkillRetryProfile,
)

STUB_SKILL_FEATURE_KEY = "stub_skill"
STUB_SKILL_NOOP_JOB_TYPE = "stub_skill.noop"


class StubSkillFeature:
    feature_key = STUB_SKILL_FEATURE_KEY

    def register_api(self, app: FastAPI) -> None:
        # Stub has no HTTP surface.
        _ = app

    def register_runtime(self) -> None:
        return None

    def register_workers(self) -> None:
        return None

    def get_manifest(self) -> SkillManifestBase:
        return SkillManifestBase(
            feature_key=STUB_SKILL_FEATURE_KEY,
            title="Stub Skill (Phase 0 canary)",
            description=(
                "Platform canary skill. Exercises the skill platform "
                "contract end-to-end (manifest, lifecycle state, "
                "skill_run creation, retention cleanup) without any "
                "real domain behaviour."
            ),
            manifest_version=1,
            config_schema_version=1,
            ai_schema_version=1,
            default_status=SkillLifecycleStatus.active,
            supports_ai=False,
            supports_runtime=True,
            supports_user_surfaces=False,
            supports_per_tenant_config=False,
            required_secrets=[],
            default_retry_profile=SkillRetryProfile(max_attempts=1),
            default_retention_profile=SkillRetentionProfile(),
        )


async def _noop_handler(
    session: Session,
    *,
    job: Job,
    run_id: str,
    attempt_id: str,
) -> None:
    """No-op handler — exists purely so the platform records a run/attempt."""
    _ = (session, job, run_id, attempt_id)


_feature_module = StubSkillFeature()


def register(registry: FeatureRegistry) -> None:
    registry.register(_feature_module)
    registry.register_skill_job_handler(
        feature_key=STUB_SKILL_FEATURE_KEY,
        job_type=STUB_SKILL_NOOP_JOB_TYPE,
        handler=_noop_handler,
    )
