"""Skill-platform job processor (spec 36 Stage 2 + spec 47/48 glue).

Polls the ``jobs`` table for rows whose ``job_type`` matches a handler
registered via ``FeatureRegistry.register_skill_job_handler`` and runs
each one inside a ``skill_run`` + ``skill_run_attempt`` wrapper.

This is deliberately simple:
  * CAS-claim via ``UPDATE jobs SET status='running' WHERE id=? AND status='pending'``
  * one attempt per job (retries handled by higher-level enqueuers for now)
  * success → job.status='completed', run.status='succeeded'
  * exception → job.status='failed', run.status='failed'

Job payload format is free-form JSON; the handler receives the full
``Job`` row plus ``run_id`` / ``attempt_id`` so it can record skill_events
or ai_executions if it wants.
"""

from __future__ import annotations

import json
import logging
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session

from sreda.db.models.core import Job
from sreda.db.repositories.skill_platform import SkillPlatformRepository
from sreda.features.registry import FeatureRegistry
from sreda.features.skill_contracts import SkillAttemptStatus, SkillTriggerType

logger = logging.getLogger(__name__)


class SkillPlatformJobProcessor:
    """Drives skill-platform jobs from the shared ``jobs`` table."""

    def __init__(self, session: Session, registry: FeatureRegistry) -> None:
        self.session = session
        self.registry = registry
        self.repo = SkillPlatformRepository(session)

    async def process_pending_jobs(self, *, limit: int = 20) -> int:
        skill_types = self.registry.skill_job_types()
        if not skill_types:
            return 0

        jobs = (
            self.session.query(Job)
            .filter(Job.job_type.in_(skill_types), Job.status == "pending")
            .order_by(Job.id.asc())
            .limit(limit)
            .all()
        )
        processed = 0
        for job in jobs:
            claimed = self._claim_job(job.id)
            if not claimed:
                continue
            await self._run_job(job)
            processed += 1
        return processed

    def _claim_job(self, job_id: str) -> bool:
        """CAS-claim: pending → running. Returns True iff we won the race."""
        result = self.session.execute(
            update(Job)
            .where(Job.id == job_id, Job.status == "pending")
            .values(status="running")
        )
        self.session.commit()
        return (result.rowcount or 0) > 0

    async def _run_job(self, job: Job) -> None:
        handler = self.registry.get_skill_job_handler(job.job_type)
        if handler is None:
            # Shouldn't happen — we filtered on registered types — but be defensive.
            logger.warning("skill_platform: no handler for job_type=%s", job.job_type)
            self._mark_job_status(job.id, "failed")
            return

        feature_key = handler.feature_key

        # Ensure there's a tenant_skill_state row (lazy init).
        manifest = self.registry.get_manifest(feature_key)
        if manifest is not None:
            self.repo.ensure_manifest_state(job.tenant_id, manifest)

        run = self.repo.create_skill_run(
            tenant_id=job.tenant_id,
            feature_key=feature_key,
            run_key=f"job:{job.id}",
            trigger_type=SkillTriggerType.system,
            trigger_ref=f"job_id={job.id}",
            workspace_id=job.workspace_id,
            input_json=job.payload_json,
            max_attempts=1,
        )
        self.repo.mark_skill_run_running(run.id)
        attempt = self.repo.create_skill_run_attempt(
            run_id=run.id,
            tenant_id=job.tenant_id,
            feature_key=feature_key,
            attempt_number=1,
            workspace_id=job.workspace_id,
            job_id=job.id,
            worker_id=f"skill_platform_processor:{uuid4().hex[:8]}",
        )
        self.session.commit()

        try:
            await handler.handler(
                self.session,
                job=job,
                run_id=run.id,
                attempt_id=attempt.id,
            )
        except Exception as exc:  # noqa: BLE001 — we catalog any handler failure
            logger.exception(
                "skill_platform: handler failed job_id=%s feature=%s",
                job.id,
                feature_key,
            )
            self.repo.complete_skill_run_attempt(
                attempt.id,
                status=SkillAttemptStatus.failed,
                error_class=type(exc).__name__,
                error_code="handler_exception",
                error_message_sanitized=str(exc)[:500],
            )
            self.repo.fail_skill_run(
                run.id,
                error_code="handler_exception",
                error_message_sanitized=str(exc)[:500],
            )
            self._mark_job_status(job.id, "failed")
            self.session.commit()
            return

        output_json = json.dumps({"status": "ok"}, ensure_ascii=False)
        self.repo.complete_skill_run_attempt(attempt.id, status=SkillAttemptStatus.succeeded)
        self.repo.complete_skill_run(run.id, output_json=output_json)
        self._mark_job_status(job.id, "completed")
        self.session.commit()

    def _mark_job_status(self, job_id: str, status: str) -> None:
        self.session.execute(
            update(Job).where(Job.id == job_id).values(status=status)
        )
