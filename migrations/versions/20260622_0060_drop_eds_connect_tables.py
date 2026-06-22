"""#181 Фаза B: дроп connect-слоя EDS (connect_sessions, tenant_eds_accounts).

EDS (eds_monitor) ретайрен полностью: движок (Фаза 1/2), таблицы EDS-мониторинга
(Фаза 4-A, ревизия 20260622_0059), а в этой фазе — connect-слой и billing read-path.

Здесь дроп 2 connect-таблиц + чистка осиротевших данных:
  - connect_sessions, tenant_eds_accounts — между ними ЦИКЛИЧЕСКИЙ FK
    (connect_sessions.tenant_eds_account_id -> tenant_eds_accounts.id и
    tenant_eds_accounts.last_connect_session_id -> connect_sessions.id), поэтому
    на PostgreSQL дропаем через DROP TABLE ... CASCADE (одной командой обе),
    на SQLite — ordered DROP TABLE IF EXISTS (SQLite не форсит FK по умолчанию).
  - secure_records record_type='eds_connect_payload' — данные connect-слоя,
    больше некем читаются (retention-ветка удалена) → DELETE.
  - subscription_plans plan_key IN ('eds_monitor_base','eds_monitor_extra_account')
    — EDS-планы (PLAN_SEEDS удалены из billing) → DELETE.

НЕ трогаем TenantFeature(feature_key='eds_monitor') — историю фич не чистим
(решение владельца; стоит enabled=False с Фазы 1).

Перед прогоном на проде — БЭКАП БД (обязательно). Downgrade — one-way (фича ретайрена).

Revision ID: 20260622_0060
Revises: 20260622_0059
"""
from __future__ import annotations

from alembic import op

revision = "20260622_0060"
down_revision = "20260622_0059"
branch_labels = None
depends_on = None

_EDS_PLAN_KEYS = ("eds_monitor_base", "eds_monitor_extra_account")


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1) Drop the connect-layer tables (cyclic FK between the two).
    if dialect == "postgresql":
        # CASCADE drops both at once regardless of FK direction.
        op.execute(
            "DROP TABLE IF EXISTS connect_sessions, tenant_eds_accounts CASCADE"
        )
    else:
        # SQLite (tests/local): no enforced FK by default → ordered drop is safe.
        op.execute("DROP TABLE IF EXISTS connect_sessions")
        op.execute("DROP TABLE IF EXISTS tenant_eds_accounts")

    # 2) Orphaned secure_records that only the connect-layer referenced
    #    (FK-safe: их единственные ссылки — из дропнутых connect_sessions/tenant_eds_accounts).
    op.execute(
        "DELETE FROM secure_records WHERE record_type = 'eds_connect_payload'"
    )

    # 3) EDS subscription_plans НЕ удаляем (Codex high+medium CRITICAL R1): на них есть FK без
    #    cascade из tenant_subscriptions.plan_id / payment_order_items.plan_id — hard-delete
    #    упал бы на FK при наличии исторических EDS-подписок/заказов (и терял бы billing-историю).
    #    Планы остаются ИНЕРТНЫМИ строками (PLAN_EDS-сиды сняты, get_summary удалён → их никто не
    #    читает; renew/display пропускают eds_monitor через is_feature_disabled). Их зачистка с
    #    зависимыми billing-строками — отдельное owner-решение, вне scope.


def downgrade() -> None:
    # One-way: EDS — ретайренная фича; воссоздание connect-слоя бессмысленно.
    # Восстановление — из бэкапа БД, снятого перед прогоном (см. docstring).
    raise NotImplementedError(
        "#181 Phase B: EDS connect-layer tables dropped (feature retired). "
        "No downgrade — restore from the pre-migration DB backup if ever needed."
    )
