"""#138 Ф3-c: ENABLE ROW LEVEL SECURITY + именованные политики (PG-only).

Механика: Ф1 (0078) создала не-owner роли sreda_app / sreda_identity / sreda_maintenance
(все NOINHERIT NOBYPASSRLS). RLS ограничивает ВСЕ не-owner роли без политики → политики
нужны и app, и maintenance, и identity. Контекст тенанта:
``current_setting('sreda.tenant_id', true)`` (missing_ok=true → NULL; сравнение с NULL =
строка невидима = fail-closed). Ставится швом db/session.py через set_config.

БЕЗ FORCE: owner (роль sreda, миграции, легаси-DSN до Ф5) обходит RLS — прод не ломается,
энфорс включается фактически на Ф5 (флип DSN на sreda_app).

NULL-tenant_id строки (nullable: secure_records, inbound_messages, react_summaries,
react_checkpoint, react_checkpoint_write) невидимы app и ДОСТУПНЫ maintenance —
owner-решение №2; покрывается той же политикой автоматически.

Списки таблиц — СТАТИЧЕСКИЕ константы модуля (миграции самодостаточны); дрейф ловит
мета-тест tests/unit/test_138_rls_registry.py (новая таблица без классификации здесь =
красный тест).

PG-guard: unit-БД SQLite → no-op (прецедент 0078/0080/0081).
"""

from __future__ import annotations

from alembic import op

revision = "20260709_0082"
down_revision = "20260709_0081"
branch_labels = None
depends_on = None


# ── Классификация таблиц (сверено с Base.metadata мета-тестом test_138_rls_registry) ──

#: Таблицы со своим tenant_id — политика изоляции app + allow-all maintenance.
TENANT_TABLES = (
    "workspaces",
    "tenant_features",
    "users",
    "assistants",
    "jobs",
    "outbox_messages",
    "secure_records",
    "inbound_messages",
    "assistant_memories",
    "memory_categories",
    "web_search_usage",
    "reply_button_cache",
    "tool_operation_results",
    "tenant_billing_cycles",
    "tenant_subscriptions",
    "payment_orders",
    "plan_library_entries",
    "tenant_skill_states",
    "tenant_skill_configs",
    "skill_runs",
    "skill_run_attempts",
    "skill_events",
    "skill_ai_executions",
    "react_turn_trace",
    "react_summaries",
    "tasks_items",
    "usage_ledger",
    "inbound_events",
    "tenant_user_profiles",
    "tenant_user_profile_proposals",
    "tenant_user_skill_configs",
    "agent_threads",
    "agent_runs",
    "user_data_change_feed",
    "audit_outbox",
    "planner_executions",
    "planner_gaps",
    "planner_llm_reservations",
    "free_tier_usage",
    "shopping_list_items",
    "recipes",
    "menu_plans",
    "family_reminders",
    "family_members",
    "message_jobs",
    "fetch_url_usage",
    "channel_link_tokens",
    "conversation_turns",
    "checklists",
    "checklist_items",
    # денормлённые дети Ф3-a (0080):
    "recipe_ingredients",
    "menu_plan_items",
    "payment_order_items",
    # денормлённые Ф3-b (0081):
    "react_checkpoint",
    "react_checkpoint_write",
)

#: Корневая таблица тенантов: app видит ТОЛЬКО свою строку (SELECT), identity — INSERT.
ROOT_TABLES = ("tenants",)

#: Без ENABLE RLS — осознанные исключения (каждое с причиной).
NO_RLS_TABLES = (
    "subscription_plans",  # public-справочник тарифов, без tenant_id
    "audit_log",  # admin: app-гранты отозваны Ф1 (0078)
    "admin_dashboard_snapshots",  # admin: app-гранты отозваны Ф1 (0078)
    "admin_alerts_seen",  # admin: app-гранты отозваны Ф1 (0078)
    "admin_sessions",  # admin-auth #305 (0077): серверные сессии по tg_id, без tenant_id
    "admin_login_challenges",  # admin-auth #305 (0077): challenge-журнал, без tenant_id
    "runtime_config",  # operational: глобальный конфиг
    "signup_attempts",  # operational: identity пишет журнал попыток без RLS
    "poller_offsets",  # operational: состояние поллеров
    "poller_heartbeats",  # operational: heartbeat поллеров
    "step_execution_ledger",  # legacy plan-execute, owner-исключение (заморожена)
)

#: Таблицы, куда sreda_identity имеет INSERT-грант (Ф1, провижн-bundle) — нужна
#: INSERT-политика (tenants — в ROOT-блоке ниже). signup_attempts в NO_RLS.
IDENTITY_INSERT_TABLES = (
    "tenants",
    "workspaces",
    "users",
    "assistants",
    "tenant_features",
    "tenant_subscriptions",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # RLS/политики — только Postgres; SQLite unit-DB не поддерживает

    # Лок-профиль (M10). ENABLE RLS + CREATE POLICY = короткий ACCESS EXCLUSIVE
    # на каталог, БЕЗ скана таблицы (ни строки не читается) — сами по себе
    # дёшевы, CONCURRENTLY тут неприменим. Единственный риск — очередь за
    # долгой транзакцией на ~56 таблицах; lock_timeout не даёт зависнуть в
    # ожидании (упадём и повторим, а не застопорим прод).
    op.execute("SET lock_timeout = '5s'")

    # 1. Тенантные таблицы: изоляция app + allow-all maintenance.
    for t in TENANT_TABLES:
        # БЕЗ FORCE — owner обходит (миграции / легаси-DSN до Ф5).
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;")
        op.execute(
            f"CREATE POLICY p_{t}_tenant ON {t} FOR ALL TO sreda_app\n"
            f"  USING (tenant_id = current_setting('sreda.tenant_id', true))\n"
            f"  WITH CHECK (tenant_id = current_setting('sreda.tenant_id', true));"
        )
        op.execute(
            f"CREATE POLICY p_{t}_maintenance ON {t} FOR ALL TO sreda_maintenance "
            f"USING (true) WITH CHECK (true);"
        )

    # 2. ROOT tenants: app видит только СВОЮ строку, maintenance — всё, identity — INSERT.
    op.execute("ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;")
    op.execute(
        "CREATE POLICY p_tenants_self ON tenants FOR SELECT TO sreda_app "
        "USING (id = current_setting('sreda.tenant_id', true));"
    )
    op.execute(
        "CREATE POLICY p_tenants_maintenance ON tenants FOR ALL TO sreda_maintenance "
        "USING (true) WITH CHECK (true);"
    )
    op.execute(
        "CREATE POLICY p_tenants_identity_insert ON tenants FOR INSERT "
        "TO sreda_identity WITH CHECK (true);"
    )

    # 3. identity INSERT-политики на остальные bundle-таблицы (ДОПОЛНИТЕЛЬНО к
    #    политикам app/maintenance из п.1; политики аддитивны — OR).
    for t in IDENTITY_INSERT_TABLES:
        if t == "tenants":
            continue  # уже в п.2
        op.execute(
            f"CREATE POLICY p_{t}_identity_insert ON {t} FOR INSERT "
            f"TO sreda_identity WITH CHECK (true);"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("SET lock_timeout = '5s'")

    # Зеркально: сперва identity-политики, потом ROOT, потом тенантные.
    for t in reversed(IDENTITY_INSERT_TABLES):
        if t == "tenants":
            continue
        op.execute(f"DROP POLICY IF EXISTS p_{t}_identity_insert ON {t};")

    op.execute("DROP POLICY IF EXISTS p_tenants_identity_insert ON tenants;")
    op.execute("DROP POLICY IF EXISTS p_tenants_maintenance ON tenants;")
    op.execute("DROP POLICY IF EXISTS p_tenants_self ON tenants;")
    op.execute("ALTER TABLE tenants DISABLE ROW LEVEL SECURITY;")

    for t in reversed(TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS p_{t}_maintenance ON {t};")
        op.execute(f"DROP POLICY IF EXISTS p_{t}_tenant ON {t};")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY;")
