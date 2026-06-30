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

from sreda.services.billing import PLAN_SEEDS, BillingService


def test_ensure_default_plans_no_engine_seeds_after_eds_removal() -> None:
    # #181 Phase B: the only engine-level PLAN_SEEDS were the EDS plans, which
    # are removed. The engine now seeds NO plans (voice / housewife are seeded
    # by their own skill modules), so PLAN_SEEDS is empty.
    assert PLAN_SEEDS == ()


def test_ensure_default_plans_no_write_when_unchanged(db_session) -> None:
    svc = BillingService(db_session)
    svc.ensure_default_plans()  # no-op now (empty PLAN_SEEDS)
    # A second call must enqueue no INSERT/UPDATE — the prod-499 regression
    # guard. With empty PLAN_SEEDS this holds trivially, but the assertion keeps
    # the contract pinned for any future engine seed.
    svc.ensure_default_plans()
    assert not db_session.new, f"unexpected INSERTs: {db_session.new}"
    assert not db_session.dirty, f"unexpected UPDATEs: {db_session.dirty}"
