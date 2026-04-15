"""Phase 0 acceptance tests for the skill platform (specs 47-48 + 41).

Three scenarios, per the roadmap acceptance criteria:

1. A stub skill registered via ``SREDA_FEATURE_MODULES`` exposes a manifest
   and its ``tenant_skill_state`` row gets lazily created.
2. A pending ``Job`` of the stub skill's registered type flows through
   ``SkillPlatformJobProcessor`` and creates ``skill_run`` +
   ``skill_run_attempt`` rows, then marks the job completed.
3. ``cleanup_runtime_retention`` deletes old terminal skill_runs (and
   their attempts/events/ai_executions), respecting live runs.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Job, Tenant, Workspace
from sreda.db.models.skill_platform import (
    SkillEvent,
    SkillRun,
    SkillRunAttempt,
    TenantSkillState,
)
from sreda.db.repositories.skill_platform import SkillPlatformRepository
from sreda.features.registry import FeatureRegistry
from sreda.features.skill_contracts import (
    SkillEventSeverity,
    SkillLifecycleStatus,
    SkillRunStatus,
)
from sreda.features.stub_skill import (
    STUB_SKILL_FEATURE_KEY,
    STUB_SKILL_NOOP_JOB_TYPE,
    StubSkillFeature,
    _noop_handler,
)
from sreda.maintenance.retention_cleanup import (
    SKILL_RUNS_DAYS,
    cleanup_runtime_retention,
)
from sreda.workers.skill_platform_processor import SkillPlatformJobProcessor


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    sess.add(Tenant(id="t1", name="Tenant 1"))
    sess.add(Workspace(id="w1", tenant_id="t1", name="Workspace 1"))
    sess.commit()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture()
def stub_registry():
    registry = FeatureRegistry()
    stub = StubSkillFeature()
    registry.register(stub)
    registry.register_skill_job_handler(
        feature_key=STUB_SKILL_FEATURE_KEY,
        job_type=STUB_SKILL_NOOP_JOB_TYPE,
        handler=_noop_handler,
    )
    return registry


# ---------------------------------------------------------------------------
# 1. Manifest + lazy tenant_skill_state creation
# ---------------------------------------------------------------------------


def test_stub_skill_manifest_creates_tenant_state(session, stub_registry):
    manifests = stub_registry.iter_manifests()
    assert any(m.feature_key == STUB_SKILL_FEATURE_KEY for m in manifests)

    stub_registry.initialize_tenant_skill_states(session, tenant_id="t1")
    session.commit()

    state = (
        session.query(TenantSkillState)
        .filter_by(tenant_id="t1", feature_key=STUB_SKILL_FEATURE_KEY)
        .one()
    )
    assert state.lifecycle_status == SkillLifecycleStatus.active.value
    assert state.health_status == "healthy"


# ---------------------------------------------------------------------------
# 2. Job → skill_run + skill_run_attempt
# ---------------------------------------------------------------------------


def test_skill_platform_processor_creates_run_and_attempt(session, stub_registry):
    job = Job(
        id=f"job_{uuid4().hex[:24]}",
        tenant_id="t1",
        workspace_id="w1",
        job_type=STUB_SKILL_NOOP_JOB_TYPE,
        status="pending",
        payload_json="{}",
    )
    session.add(job)
    session.commit()

    processor = SkillPlatformJobProcessor(session, stub_registry)
    processed = asyncio.run(processor.process_pending_jobs(limit=10))
    assert processed == 1

    session.expire_all()
    refreshed_job = session.get(Job, job.id)
    assert refreshed_job.status == "completed"

    runs = session.query(SkillRun).filter_by(feature_key=STUB_SKILL_FEATURE_KEY).all()
    assert len(runs) == 1
    run = runs[0]
    assert run.status == SkillRunStatus.succeeded.value
    assert run.tenant_id == "t1"
    assert run.run_key == f"job:{job.id}"

    attempts = session.query(SkillRunAttempt).filter_by(run_id=run.id).all()
    assert len(attempts) == 1
    assert attempts[0].status == "succeeded"
    assert attempts[0].job_id == job.id

    # Lazy tenant_skill_state was created by the processor.
    state = (
        session.query(TenantSkillState)
        .filter_by(tenant_id="t1", feature_key=STUB_SKILL_FEATURE_KEY)
        .one()
    )
    assert state.last_run_id == run.id
    assert state.last_successful_run_at is not None


# ---------------------------------------------------------------------------
# 3. Retention cleanup deletes old terminal runs but preserves live ones
# ---------------------------------------------------------------------------


def test_retention_cleanup_deletes_old_skill_runs(session):
    repo = SkillPlatformRepository(session)
    now = datetime.now(timezone.utc)
    ancient = now - timedelta(days=SKILL_RUNS_DAYS + 10)
    recent = now - timedelta(days=1)

    # Old succeeded run — should be deleted along with its attempt + event.
    old_run = repo.create_skill_run(
        tenant_id="t1",
        feature_key=STUB_SKILL_FEATURE_KEY,
        run_key="old",
    )
    repo.mark_skill_run_running(old_run.id)
    repo.complete_skill_run(old_run.id)
    old_run.created_at = ancient
    old_run.finished_at = ancient

    old_attempt = repo.create_skill_run_attempt(
        run_id=old_run.id,
        tenant_id="t1",
        feature_key=STUB_SKILL_FEATURE_KEY,
        attempt_number=1,
    )
    old_attempt.created_at = ancient

    old_event = repo.append_skill_event(
        tenant_id="t1",
        feature_key=STUB_SKILL_FEATURE_KEY,
        severity=SkillEventSeverity.info,
        event_type="stub.old",
        message="old",
        run_id=old_run.id,
        attempt_id=old_attempt.id,
    )
    old_event.created_at = ancient

    # Recent, still-running run — must survive cleanup.
    live_run = repo.create_skill_run(
        tenant_id="t1",
        feature_key=STUB_SKILL_FEATURE_KEY,
        run_key="live",
    )
    repo.mark_skill_run_running(live_run.id)
    live_run.created_at = recent

    # Recent succeeded run — must also survive (within window).
    fresh_run = repo.create_skill_run(
        tenant_id="t1",
        feature_key=STUB_SKILL_FEATURE_KEY,
        run_key="fresh",
    )
    repo.complete_skill_run(fresh_run.id)
    fresh_run.created_at = recent

    session.commit()

    result = cleanup_runtime_retention(session)
    session.commit()

    assert result.skill_runs == 1
    assert result.skill_run_attempts == 1
    assert result.skill_events_debug_info == 1

    surviving_run_keys = {
        r.run_key for r in session.query(SkillRun).all()
    }
    assert surviving_run_keys == {"live", "fresh"}
    assert session.query(SkillRunAttempt).count() == 0
    assert session.query(SkillEvent).count() == 0
