#!/usr/bin/env python3
"""#204 Фаза 3 — снять 2 легаси voice-подписки (DATA, форвард-онли, бэкап-гейт).

Контекст. После слияния ``voice_transcription`` в основной тариф (Ф1: расход
голоса пишется под carrier-агентом ``housewife_assistant``; Ф2: ВСЕ пути
реактивации мёртвого плана ``voice_transcription_base`` закрыты) на проде
остались РОВНО 2 тенанта со старой отдельной voice-подпиской.

(Реальные ``Tenant.id`` = ``tenant_tg_<telegram_id>``, см.
``services/onboarding.py:292``. Конкретные id — PII и НЕ коммитятся
(audit 2026-07-18): оператор передаёт их через env
``SREDA_204_EXPECTED_TENANTS`` — comma-separated, напр.
``tenant_tg_111,tenant_tg_222`` — из deploy-заметок задачи #204.)
У обоих параллельно active ``housewife_assistant``
— именно он теперь несёт голос (атрибуция Ф1). Эта подписка снимает их легаси
voice-подписки, чтобы в /admin/budget не осталось отдельной active voice-строки;
голос продолжает работать (resolve → housewife).

Почему СТАНДАЛОН-СКРИПТ, а не Alembic-миграция. Это прод-СПЕЦИФИЧНЫЕ данные
(2 конкретных тенанта). Миграция гонялась бы и на dev/test, где этих строк нет,
и её «снять ровно 2» либо был бы no-op (бессмысленно), либо потребовал бы тех же
гардов — но миграции деплоятся автоматически на каждой среде, а здесь нужен
ручной запуск под бэкап-гейтом. Поэтому — отдельный скрипт, запускаемый оператором
вручную ОДИН раз на проде.

Безопасность (Только Вперёд, Tombstone не delete):
  * Hard preconditions в ОДНОЙ транзакции: ровно 2 active voice-подписки И
    tenant-set РОВНО ``SREDA_204_EXPECTED_TENANTS`` И у ОБОИХ active
    housewife. Любое расхождение (дрейф прода) → печать diagnostics + STOP БЕЗ
    мутаций (rollback).
  * Мутация зеркалит ``BillingService.cancel_voice_subscription`` (billing.py):
    ``status='cancelled'``, ``quantity=0``, ``next_cycle_quantity=0``,
    ``cancel_at_period_end=True``, ``updated_at=now`` + feature-flag
    ``voice_transcription`` → disabled (mirror ``_ensure_feature_enabled(...False)``).
  * Идемпотентно: повторный прогон (уже 0 active voice) не пройдёт precondition
    «ровно 2 active» → чистый no-op (без исключения).
  * Бэкап-гейт (V8): перед мутацией требуется свежий прод-дамп (монитор
    ``probe_last_backup_age`` зелёный, возраст < 30ч) ЛИБО оператор подтверждает
    ручной dump в эту сессию через ``--i-have-a-fresh-backup``.

Usage:

    # Обязательный env: точный tenant-set (НЕ коммитить, брать из
    # deploy-заметок #204):
    export SREDA_204_EXPECTED_TENANTS="tenant_tg_<id1>,tenant_tg_<id2>"

    # Прод-проверка БЕЗ записи (печатает, что СДЕЛАЛ БЫ):
    python scripts/204_cancel_legacy_voice_subs.py --dry-run

    # Реальный прогон (оператор УЖЕ сделал свежий дамп):
    #   1) монитор бэкапа зелёный  → гейт пройдёт сам;
    #   2) либо подтвердить вручную → --i-have-a-fresh-backup
    python scripts/204_cancel_legacy_voice_subs.py

    # Под sreda service-аккаунтом, env из /etc/sreda/.env:
    sudo systemd-run --uid=sreda --gid=sreda \\
        --working-directory=/opt/sreda \\
        -p EnvironmentFile=/etc/sreda/.env \\
        --wait --collect --pipe \\
        /opt/sreda/.venv/bin/python \\
        /opt/sreda/scripts/204_cancel_legacy_voice_subs.py
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from sreda.db.models.billing import SubscriptionPlan, TenantSubscription
from sreda.db.models.core import TenantFeature
from sreda.services.agent_capabilities import resolve_voice_access
from sreda.services.billing import PLAN_VOICE_TRANSCRIPTION

# ---------------------------------------------------------------------------
# Constants — the production fingerprint this script is allowed to touch.
# ---------------------------------------------------------------------------

# plan_key of the legacy standalone voice plan. This is the SAME plan
# ``BillingService.cancel_voice_subscription`` targets — it resolves the row
# via ``_get_subscription_optional(tenant, PLAN_VOICE_TRANSCRIPTION)``, i.e. by
# plan_key, NOT by feature_key (billing.py:513). We mirror that exactly so we
# can never touch a *different* plan that happens to share the
# ``voice_transcription`` feature_key. (Imported from billing so the literal
# can't drift from the production value.)
VOICE_PLAN_KEY = PLAN_VOICE_TRANSCRIPTION  # "voice_transcription_base"

# feature_key of the legacy standalone voice plan — used ONLY for the
# TenantFeature flag mirror (``_ensure_feature_enabled(tenant,
# 'voice_transcription', False)`` in cancel_voice_subscription). NOT used to
# select subscriptions (that goes by plan_key above).
VOICE_FEATURE_KEY = "voice_transcription"

# Canonical production tenant ids — PII, НЕ коммитятся (audit 2026-07-18).
# Передаются через env ``SREDA_204_EXPECTED_TENANTS`` (comma-separated
# ``tenant_tg_<telegram_id>``, формат см. onboarding.py:292
# ``tenant_id = f"tenant_tg_{telegram_id}"``). Пустой set = fail-fast в main()
# (а чистая функция безопасно остановится на precondition-2 без мутаций).
EXPECTED_TENANTS_ENV = "SREDA_204_EXPECTED_TENANTS"


def _load_expected_tenant_ids() -> frozenset[str]:
    raw = os.environ.get(EXPECTED_TENANTS_ENV, "")
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


EXPECTED_TENANT_IDS: frozenset[str] = _load_expected_tenant_ids()


# ---------------------------------------------------------------------------
# Result of the pure mutation function.
# ---------------------------------------------------------------------------


@dataclass
class CancelResult:
    """Outcome of one ``cancel_legacy_voice_subs`` call.

    ``applied``           — True iff the precondition passed AND mutation was
                            committed (dry_run=False).
    ``cancelled_count``   — voice subs actually cancelled+committed.
    ``would_cancel_count``— voice subs that WOULD be cancelled (dry-run only).
    ``cancelled_tenant_ids`` — tenant ids whose voice sub was (or would be)
                            cancelled.
    ``reason``            — human-readable precondition / drift explanation.
    """

    applied: bool = False
    cancelled_count: int = 0
    would_cancel_count: int = 0
    cancelled_tenant_ids: list[str] = field(default_factory=list)
    reason: str = ""


# ---------------------------------------------------------------------------
# Pure mutation function (no env, no I/O) — unit-tested on synthetic data.
# ---------------------------------------------------------------------------


def _active_voice_subs(session: Session) -> list[TenantSubscription]:
    """All active subscriptions on the legacy voice plan, joined to the plan so
    we only ever touch the deprecated ``voice_transcription_base`` plan — NOT
    "any voice-feature plan". We filter by ``plan_key`` to mirror
    ``cancel_voice_subscription`` exactly (it resolves the row by
    ``PLAN_VOICE_TRANSCRIPTION`` plan_key, billing.py:513). Filtering by
    ``feature_key`` instead would also match a *different* future plan sharing
    the ``voice_transcription`` feature_key and could cancel the wrong row."""
    return (
        session.query(TenantSubscription)
        .join(SubscriptionPlan, TenantSubscription.plan_id == SubscriptionPlan.id)
        .filter(
            TenantSubscription.status == "active",
            SubscriptionPlan.plan_key == VOICE_PLAN_KEY,
        )
        .order_by(TenantSubscription.tenant_id)
        .all()
    )


def _ensure_voice_feature_disabled(session: Session, tenant_id: str) -> None:
    """Mirror ``BillingService._ensure_feature_enabled(tenant, 'voice_transcription',
    False)``: flip an existing row to disabled, or create one disabled."""
    feature = (
        session.query(TenantFeature)
        .filter(
            TenantFeature.tenant_id == tenant_id,
            TenantFeature.feature_key == VOICE_FEATURE_KEY,
        )
        .one_or_none()
    )
    if feature is None:
        session.add(
            TenantFeature(
                id=f"{tenant_id}:{VOICE_FEATURE_KEY}",
                tenant_id=tenant_id,
                feature_key=VOICE_FEATURE_KEY,
                enabled=False,
            )
        )
        return
    feature.enabled = False


def cancel_legacy_voice_subs(
    session: Session,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> CancelResult:
    """Cancel the 2 legacy voice subscriptions, idempotently, behind hard
    preconditions checked in this session's transaction.

    Preconditions (ALL must hold, else STOP with NO mutation):
      1. Exactly 2 active voice subs on the ``voice_transcription_base`` plan.
      2. Their tenant-set is EXACTLY ``EXPECTED_TENANT_IDS``.
      3. For BOTH tenants ``resolve_voice_access(session, tid).allowed`` is True
         — i.e. voice REALLY works through a manifest carrier
         (``includes_voice=True``, e.g. ``housewife_assistant``), NOT through
         the legacy voice sub (resolve ignores the legacy plan). A status-only
         "housewife active" check is insufficient: a degraded housewife
         (quantity=0 or expired ``active_until``) would pass it yet leave the
         tenant with NO working voice once we cancel the legacy sub. We gate on
         the real entitlement instead.

    On the happy path each voice sub is mutated to mirror
    ``cancel_voice_subscription`` and (unless ``dry_run``) committed. Because
    ``resolve_voice_access`` ignores the legacy voice plan entirely, cancelling
    it must NOT change ``.allowed`` — so the postconditions assert (before
    commit) active voice == 0 AND both tenants STILL have
    ``resolve_voice_access(...).allowed`` True. Any violation rolls back and
    raises.

    Idempotent: a second run sees 0 active voice subs → precondition (1) fails
    → clean no-op (``applied=False``), no exception.
    """
    current_time = now or datetime.now(UTC)

    voice_subs = _active_voice_subs(session)
    tenant_ids = sorted(s.tenant_id for s in voice_subs)

    # --- Precondition 1: exactly 2 active voice subs -----------------------
    if len(voice_subs) != 2:
        return CancelResult(
            applied=False,
            reason=(
                f"precondition FAIL: expected exactly 2 active voice subs, "
                f"found {len(voice_subs)} (tenants={tenant_ids}). "
                "No mutation. If this run is AFTER a successful one, 0 active "
                "voice subs is the expected idempotent no-op."
            ),
        )

    # --- Precondition 2: tenant-set is exactly the expected pair -----------
    actual_set = frozenset(tenant_ids)
    if actual_set != EXPECTED_TENANT_IDS:
        return CancelResult(
            applied=False,
            reason=(
                "precondition FAIL: tenant-set drift. "
                f"expected={sorted(EXPECTED_TENANT_IDS)} actual={tenant_ids}. "
                "No mutation."
            ),
        )

    # --- Precondition 3: voice REALLY works through a manifest carrier -----
    # resolve_voice_access(...).allowed is the real entitlement (active carrier
    # sub with includes_voice=True, quantity>0, active_until future-or-NULL). It
    # ignores the legacy voice sub, so a degraded housewife (quantity=0 /
    # expired) yields allowed=False here → STOP, never stranding the tenant.
    stranded = [
        t for t in tenant_ids if not resolve_voice_access(session, t).allowed
    ]
    if stranded:
        return CancelResult(
            applied=False,
            reason=(
                "precondition FAIL: tenant(s) whose voice does NOT resolve "
                f"through a carrier (resolve_voice_access.allowed=False): {stranded}. "
                "Cancelling the legacy voice sub would leave them WITHOUT voice. "
                "No mutation."
            ),
        )

    # --- Mutation (mirror cancel_voice_subscription) -----------------------
    for sub in voice_subs:
        sub.status = "cancelled"
        sub.quantity = 0
        sub.next_cycle_quantity = 0
        sub.cancel_at_period_end = True
        sub.updated_at = current_time
        _ensure_voice_feature_disabled(session, sub.tenant_id)

    if dry_run:
        # Self-contained contract: a dry-run makes NO durable mutation BY
        # ITSELF — roll back the in-flight ORM/feature-flag changes here, so a
        # caller (or test) doesn't have to remember an external rollback. Report
        # what WOULD happen.
        session.rollback()
        return CancelResult(
            applied=False,
            would_cancel_count=len(voice_subs),
            cancelled_tenant_ids=tenant_ids,
            reason=f"DRY-RUN: would cancel {len(voice_subs)} voice subs {tenant_ids}.",
        )

    session.flush()

    # --- Postconditions (assert in this session BEFORE commit) -------------
    remaining_active_voice = len(_active_voice_subs(session))
    if remaining_active_voice != 0:
        session.rollback()
        raise AssertionError(
            f"postcondition FAIL: {remaining_active_voice} active voice subs "
            "remain after cancel — rolled back."
        )
    for t in tenant_ids:
        if not resolve_voice_access(session, t).allowed:
            session.rollback()
            raise AssertionError(
                f"postcondition FAIL: tenant {t} lost voice access "
                "(resolve_voice_access.allowed=False) after cancel — rolled back. "
                "Cancelling the legacy voice sub must NOT change resolve "
                "(it ignores the legacy plan)."
            )

    session.commit()
    return CancelResult(
        applied=True,
        cancelled_count=len(voice_subs),
        cancelled_tenant_ids=tenant_ids,
        reason=f"cancelled {len(voice_subs)} voice subs {tenant_ids}.",
    )


# ---------------------------------------------------------------------------
# Backup gate (V8) — prod-only; pure function above is gate-free.
# ---------------------------------------------------------------------------


def _backup_gate_ok(*, operator_confirmed: bool) -> tuple[bool, str]:
    """Return (ok, message). The data mutation must NOT start without a fresh
    prod dump. Two ways to satisfy the gate:

      1. ``probe_last_backup_age`` reports ``ok`` (latest encrypted dump < 30h),
         OR
      2. the operator passed ``--i-have-a-fresh-backup`` (manual dump this
         session).

    Off prod the backup dir doesn't exist → probe is ``critical`` → gate fails
    closed, which is correct: a dry-run is the right way to inspect off prod.
    """
    if operator_confirmed:
        return True, "backup gate: operator confirmed a fresh dump (--i-have-a-fresh-backup)."
    try:
        from scripts.monitor_health import probe_last_backup_age  # type: ignore
    except Exception:  # pragma: no cover - import shape varies off prod
        try:
            # When run as ``python scripts/204_...py`` the sibling import is by
            # bare module name.
            from monitor_health import probe_last_backup_age  # type: ignore
        except Exception as exc:
            return (
                False,
                f"backup gate: could not load probe_last_backup_age ({exc!r}); "
                "pass --i-have-a-fresh-backup after taking a manual dump.",
            )
    result = probe_last_backup_age()
    if getattr(result, "status", None) == "ok":
        return True, f"backup gate: monitor green — {result.message}"
    return (
        False,
        f"backup gate: monitor NOT green — {getattr(result, 'message', result)}. "
        "Take a fresh dump and re-run (or pass --i-have-a-fresh-backup).",
    )


# ---------------------------------------------------------------------------
# CLI entrypoint (prod) — wires env-backed session + backup gate.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0],
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what WOULD be cancelled; commit nothing. Skips the backup gate.",
    )
    parser.add_argument(
        "--i-have-a-fresh-backup",
        dest="fresh_backup",
        action="store_true",
        help="Operator attests a fresh prod dump was taken this session "
        "(satisfies the backup gate when the monitor isn't reachable).",
    )
    args = parser.parse_args(argv)

    # Fail-fast без tenant-set: иначе precondition-2 просто отклонит прогон,
    # и оператор может неверно прочитать это как «дрейф прода».
    if not EXPECTED_TENANT_IDS:
        print(
            f"ABORT: {EXPECTED_TENANTS_ENV} is not set. Provide the exact "
            "comma-separated tenant ids out-of-band (never commit them)."
        )
        return 2

    # The session factory reads DATABASE_URL etc. from the environment
    # (settings) — on prod via EnvironmentFile=/etc/sreda/.env.
    from sreda.db.session import get_session_factory

    if not args.dry_run:
        ok, msg = _backup_gate_ok(operator_confirmed=args.fresh_backup)
        print(msg)
        if not ok:
            print("ABORT: backup gate not satisfied. No mutation.")
            return 2
    else:
        print("DRY-RUN: backup gate skipped (no mutation will be committed).")

    session = get_session_factory()()
    try:
        result = cancel_legacy_voice_subs(
            session, dry_run=args.dry_run
        )
    finally:
        if args.dry_run:
            # Defensive only: cancel_legacy_voice_subs already rolls back its
            # own in-flight changes on dry_run (self-contained contract). A
            # second rollback on an already-clean session is a harmless no-op.
            session.rollback()
        session.close()

    print(result.reason)
    if args.dry_run:
        print(
            f"DRY-RUN result: would_cancel={result.would_cancel_count} "
            f"tenants={result.cancelled_tenant_ids}"
        )
        return 0
    if result.applied:
        print(
            f"DONE: cancelled={result.cancelled_count} "
            f"tenants={result.cancelled_tenant_ids}"
        )
        return 0
    # Not applied and not dry-run → precondition failed (drift or idempotent
    # no-op). Exit non-zero on real drift, zero on the idempotent no-op.
    if "0 active" in result.reason or "found 0" in result.reason:
        print("NO-OP: already cancelled (idempotent). Nothing to do.")
        return 0
    print("STOP: precondition failed (drift). No mutation. Investigate above.")
    return 3


if __name__ == "__main__":
    sys.exit(main())
