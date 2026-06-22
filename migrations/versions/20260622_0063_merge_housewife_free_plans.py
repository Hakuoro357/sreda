"""#200 Фаза 2 — слияние free-тарифов домохозяйки.

Переносит активные подписки `plan_housewife_base` → `plan_sreda_free` (сохраняя
`grandfathered_at` — у всех 15 уже проставлен миграцией 0041), затем дропает legacy-планы
`plan_housewife_base` + `plan_housewife_grandfathered` при отсутствии FK-ссылок.

Префлайт прода (2026-06-22): 15 active на housewife_base (ВСЕ grandfathered), 0 order_items на
обоих планах, дублей active нет. Источник plan-строк — миграции 0017/0041, рантайм-сидера нет
(`billing.PLAN_SEEDS==()`) → DROP безопасен (forward upgrade head не воскресит). Гейты тарифа
спасают 15 через `is_grandfathered` (меняется только plan_id). `signup_abuse` исключает
grandfathered из free-cap (правка в коде, не здесь).

Идемпотентно: повторный upgrade → UPDATE матчит 0 строк (этого plan_id уже нет), DROP — по
факту ссылок. FK `tenant_subscriptions.plan_id`/`payment_order_items.plan_id` БЕЗ cascade →
hard-delete плана только при 0 ссылках (c-RET2), иначе ретайр (is_active/is_public=false).

Перед прогоном на проде — БЭКАП БД (обязательно). Downgrade — one-way.

Revision ID: 20260622_0063
Revises: 20260622_0062
"""
from __future__ import annotations

import logging

from alembic import op
from sqlalchemy import text

revision = "20260622_0063"
down_revision = "20260622_0062"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

_BASE = "plan_housewife_base"
_GRANDFATHERED = "plan_housewife_grandfathered"
_TARGET = "plan_sreda_free"
_REASON = "Migration #200: housewife_base merged into sreda_free"


def _drop_plan_if_unreferenced(conn, plan_id: str) -> None:
    """DELETE плана при 0 FK-ссылках; иначе ретайр (is_active/is_public=false) + WARN."""
    subs = conn.execute(
        text("SELECT COUNT(*) FROM tenant_subscriptions WHERE plan_id = :p"),
        {"p": plan_id},
    ).scalar() or 0
    items = conn.execute(
        text("SELECT COUNT(*) FROM payment_order_items WHERE plan_id = :p"),
        {"p": plan_id},
    ).scalar() or 0
    if subs == 0 and items == 0:
        conn.execute(
            text("DELETE FROM subscription_plans WHERE id = :p"), {"p": plan_id}
        )
        logger.info("#200 merge: план %s удалён (0 ссылок)", plan_id)
    else:
        conn.execute(
            text(
                "UPDATE subscription_plans SET is_active = :f, is_public = :f "
                "WHERE id = :p"
            ),
            {"f": False, "p": plan_id},
        )
        logger.warning(
            "#200 merge: план %s НЕ удалён (subs=%s order_items=%s) — ретайр",
            plan_id, subs, items,
        )


def _apply(conn) -> None:
    """Ядро миграции — отдельно от op.get_bind(), чтобы DDL-unit тест звал напрямую."""
    # 1) Soft preflight: лог cohort; abort ТОЛЬКО на реальной аномалии (active-дубли,
    #    которые partial-unique-index должен предотвращать). Легит. дрейф (14/15) НЕ роняем —
    #    UPDATE WHERE сам по себе партиал-устойчив и идемпотентен.
    active_base = conn.execute(
        text(
            "SELECT COUNT(*) FROM tenant_subscriptions "
            "WHERE plan_id = :p AND status = 'active'"
        ),
        {"p": _BASE},
    ).scalar() or 0
    dups = conn.execute(
        text(
            "SELECT COUNT(*) FROM (SELECT tenant_id FROM tenant_subscriptions "
            "WHERE feature_key = 'housewife_assistant' AND status = 'active' "
            "GROUP BY tenant_id HAVING COUNT(*) > 1) d"
        )
    ).scalar() or 0
    logger.info(
        "#200 merge preflight: active plan_housewife_base=%s, active housewife dups=%s",
        active_base, dups,
    )
    if dups:
        raise RuntimeError(
            f"#200 merge ABORT: {dups} тенант(ов) с >1 active housewife-подпиской — "
            "разрешить вручную перед слиянием."
        )

    # 2) Перенос plan_id base→sreda_free, сохраняя grandfathered_at (COALESCE — не перетираем).
    res = conn.execute(
        text(
            "UPDATE tenant_subscriptions "
            "SET plan_id = :t, "
            "    grandfathered_at = COALESCE(grandfathered_at, CURRENT_TIMESTAMP), "
            "    updated_at = CURRENT_TIMESTAMP, "
            "    grandfather_reason = COALESCE(grandfather_reason, :reason) "
            "WHERE plan_id = :b AND status = 'active'"
        ),
        {"t": _TARGET, "b": _BASE, "reason": _REASON},
    )
    logger.info("#200 merge: перенесено подписок base→sreda_free: %s", res.rowcount)

    # 3) Дроп legacy-планов при 0 ссылках (FK без cascade — c-RET2).
    for plan_id in (_BASE, _GRANDFATHERED):
        _drop_plan_if_unreferenced(conn, plan_id)


def upgrade() -> None:
    _apply(op.get_bind())


def downgrade() -> None:
    # One-way: слияние free-тарифов. Восстановление — из бэкапа БД, снятого перед прогоном.
    raise NotImplementedError(
        "#200 Фаза 2: слияние free-тарифов one-way — restore from the pre-migration DB backup."
    )
