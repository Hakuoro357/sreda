"""`ensure_default_plans` must NOT write on the hot read path when the plan
catalog already matches the seeds (vex-assistant#99/#100).

Prod 2026-06-04: the Mini App `/plans` + `/summary` endpoints called
`ensure_default_plans` on every open, which blind-UPDATEd the shared plan rows
on each call. Under the migration-broadcast burst, concurrent opens serialized
on those PostgreSQL row locks and timed out (nginx 499 → blank Mini App). The
method now writes only when a plan is missing or a field actually differs, so
the steady-state hot path is read-only.

Uses the shared ``db_session`` fixture (tests/unit/conftest.py) which registers
all ORM tables and rolls back per test.
"""

from __future__ import annotations

from sreda.db.models.billing import SubscriptionPlan
from sreda.services.billing import BillingService


def test_ensure_default_plans_seeds_on_first_call(db_session) -> None:
    BillingService(db_session).ensure_default_plans()
    assert db_session.query(SubscriptionPlan).count() > 0


def test_ensure_default_plans_no_write_when_unchanged(db_session) -> None:
    svc = BillingService(db_session)
    svc.ensure_default_plans()  # first call seeds + flushes
    # Second call on an already-seeded, unchanged catalog must enqueue no
    # INSERT/UPDATE — otherwise every Mini App open writes + row-locks the
    # shared plan rows (the prod 499 root cause).
    svc.ensure_default_plans()
    assert not db_session.new, f"unexpected INSERTs: {db_session.new}"
    assert not db_session.dirty, f"unexpected UPDATEs: {db_session.dirty}"


def test_ensure_default_plans_rewrites_when_field_drifts(db_session) -> None:
    svc = BillingService(db_session)
    svc.ensure_default_plans()
    plan = db_session.query(SubscriptionPlan).first()
    assert plan is not None
    plan_key = plan.plan_key
    drift = "СТАРОЕ НАЗВАНИЕ КОТОРОГО НЕТ В СИДЕ"
    plan.title = drift
    db_session.flush()
    # ensure_default_plans flushes its own correction, so assert the EFFECT
    # (the drifted row is rewritten back to match the seed).
    svc.ensure_default_plans()
    refreshed = (
        db_session.query(SubscriptionPlan)
        .filter(SubscriptionPlan.plan_key == plan_key)
        .one()
    )
    assert refreshed.title != drift, "drifted plan should be re-written to match its seed"
