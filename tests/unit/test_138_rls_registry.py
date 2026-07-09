"""#138 Ф3-c — мета-тест дрейфа RLS-классификации (fail-closed гейт).

R1 M8-фикс: реестр — источник истины в APP-модуле ``sreda.db.rls_registry`` (НЕ в
применённой миграции 0082, иначе добавление имени туда не создавало политику →
несамоисполнимо). Каждая таблица Base.metadata обязана быть классифицирована РОВНО
в одном множестве реестра. Новая таблица без классификации = красный → «классифицируй
в rls_registry + напиши миграцию ENABLE RLS». Санити: TENANT-таблица имеет tenant_id;
опечатка (мёртвое имя) ловится. Parity реестр ⇔ pg_policies на реальном PG — в red-suite.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_migration_0082():
    root = Path(__file__).resolve().parents[2]
    path = root / "migrations" / "versions" / "20260709_0082_enable_rls_policies.py"
    spec = importlib.util.spec_from_file_location("migration_0082", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _metadata_tables():
    """Все таблицы Base.metadata (НЕ __all__ — audit_log не в __all__, owner-решение №1)."""
    from sreda.db.base import Base

    import sreda.db.models  # noqa: F401 — регистрирует ядро таблиц
    # Подмодули, которые __init__ не импортирует (как в conftest _test_engine):
    import sreda.db.models.audit  # noqa: F401
    import sreda.db.models.free_tier  # noqa: F401
    import sreda.db.models.reply_buttons  # noqa: F401

    return Base.metadata.tables


# R2-фикс (high+субагент): НЕ equality с живым реестром — это возвращало правку ПРИМЕНЁННОЙ
# 0082 при каждой новой таблице (ровно дрейф из R1: имя в старой миграции ≠ политика на проде).
# Вместо этого 0082 заморожена ИСТОРИЧЕСКИМ снапшотом ЗДЕСЬ: любое редактирование применённой
# миграции = красный. Новые таблицы идут в rls_registry + НОВУЮ миграцию (0082 не трогается);
# «реестр ⇔ реальные политики» держит parity-тест red-suite на живом PG.
_FROZEN_0082_TENANT = frozenset({
    "workspaces", "tenant_features", "users", "assistants", "jobs", "outbox_messages",
    "secure_records", "inbound_messages", "assistant_memories", "memory_categories",
    "web_search_usage", "reply_button_cache", "tool_operation_results",
    "tenant_billing_cycles", "tenant_subscriptions", "payment_orders",
    "plan_library_entries", "tenant_skill_states", "tenant_skill_configs", "skill_runs",
    "skill_run_attempts", "skill_events", "skill_ai_executions", "react_turn_trace",
    "react_summaries", "tasks_items", "usage_ledger", "inbound_events",
    "tenant_user_profiles", "tenant_user_profile_proposals", "tenant_user_skill_configs",
    "agent_threads", "agent_runs", "user_data_change_feed", "audit_outbox",
    "planner_executions", "planner_gaps", "planner_llm_reservations", "free_tier_usage",
    "shopping_list_items", "recipes", "menu_plans", "family_reminders", "family_members",
    "message_jobs", "fetch_url_usage", "channel_link_tokens", "conversation_turns",
    "checklists", "checklist_items",
    "recipe_ingredients", "menu_plan_items", "payment_order_items",
    "react_checkpoint", "react_checkpoint_write",
})
_FROZEN_0082_NO_RLS = frozenset({
    "subscription_plans", "audit_log", "admin_dashboard_snapshots", "admin_alerts_seen",
    "admin_sessions", "admin_login_challenges", "runtime_config", "signup_attempts",
    "poller_offsets", "poller_heartbeats", "step_execution_ledger",
})
_FROZEN_0082_IDENTITY_INSERT = frozenset({
    "tenants", "workspaces", "users", "assistants", "tenant_features", "tenant_subscriptions",
})


def test_migration_0082_is_frozen_historical_snapshot():
    """Применённую 0082 НЕ редактируют (правка = красный тест). Реестр может только
    РАСТИ относительно снапшота (новые таблицы — новыми миграциями)."""
    from sreda.db import rls_registry as reg

    mig = _load_migration_0082()
    assert set(mig.TENANT_TABLES) == _FROZEN_0082_TENANT, (
        "миграция 0082 ОТРЕДАКТИРОВАНА — так нельзя: она уже применена на проде, правка "
        "имени не создаст политику. Новая таблица → rls_registry + НОВАЯ миграция ENABLE RLS."
    )
    assert set(mig.NO_RLS_TABLES) == _FROZEN_0082_NO_RLS
    assert set(mig.ROOT_TABLES) == {"tenants"}
    # R3 MINOR (high+medium): заморозить и IDENTITY_INSERT — иначе правка identity-списка
    # применённой 0082 осталась бы зелёной (provisioning-дыра: fresh PG получит политику,
    # прод после уже-применённой 0082 — нет).
    assert set(mig.IDENTITY_INSERT_TABLES) == _FROZEN_0082_IDENTITY_INSERT
    # Реестр ⊇ снапшот (может только расти; сокращение реестра = осознанная миграция DISABLE).
    assert _FROZEN_0082_TENANT <= reg.TENANT_TABLES, "rls_registry потерял таблицы снапшота 0082"
    assert _FROZEN_0082_IDENTITY_INSERT <= reg.IDENTITY_INSERT_TABLES


def test_rls_registry_covers_all_tables_exactly_once():
    """(а)+(б): каждая таблица метадаты ∈ ровно одному множеству app-реестра; без мёртвых имён."""
    from sreda.db import rls_registry as reg

    tenant = set(reg.TENANT_TABLES)
    root = set(reg.ROOT_TABLES)
    no_rls = set(reg.NO_RLS_TABLES)

    # Ровно одно множество на таблицу — пересечения пусты.
    assert not tenant & root, f"таблицы и в TENANT, и в ROOT: {sorted(tenant & root)}"
    assert not tenant & no_rls, f"таблицы и в TENANT, и в NO_RLS: {sorted(tenant & no_rls)}"
    assert not root & no_rls, f"таблицы и в ROOT, и в NO_RLS: {sorted(root & no_rls)}"

    classified = tenant | root | no_rls
    actual = set(_metadata_tables()) - {"alembic_version"}

    unclassified = actual - classified
    assert not unclassified, (
        "Новые таблицы БЕЗ RLS-классификации — (1) классифицируй в "
        "src/sreda/db/rls_registry.py (NO_RLS только осознанно, с причиной) и "
        "(2) для tenant-таблицы напиши НОВУЮ миграцию ENABLE RLS + политики по образцу "
        "0082 (саму 0082 НЕ редактировать — она применена; parity-тест red-suite "
        f"проверит факт политик на PG): {sorted(unclassified)}"
    )

    ghosts = classified - actual
    assert not ghosts, (
        "В rls_registry есть несуществующие таблицы (опечатка = молчаливо "
        f"незащищённая таблица; поправь имя): {sorted(ghosts)}"
    )


def test_tenant_tables_have_tenant_id_column():
    """(в): изоляционная политика бессмысленна без колонки tenant_id."""
    from sreda.db import rls_registry as reg
    tables = _metadata_tables()
    missing = sorted(
        t for t in reg.TENANT_TABLES
        if t in tables and "tenant_id" not in tables[t].columns
    )
    assert not missing, (
        f"TENANT_TABLES без колонки tenant_id (политика 0082 не сработает): {missing}"
    )


def test_root_tenants_has_id_column():
    """(г): политика p_tenants_self фильтрует по id."""
    tables = _metadata_tables()
    assert "tenants" in tables
    assert "id" in tables["tenants"].columns
