"""#138 RLS-реестр классификации таблиц — ИСТОЧНИК ИСТИНЫ (app-модуль).

R1 M8-фикс (2026-07-09): раньше реестр жил В миграции 0082 (уже применённой) —
добавление имени туда НЕ создавало политику на проде (0082 не перезапускается) →
мета-тест был несамоисполним. Теперь реестр здесь, а гарантия двухслойная:
  1. Мета-тест ``test_138_rls_registry`` сверяет ЭТОТ реестр ⇔ ``Base.metadata`` —
     новая таблица без классификации → красный (drift-гейт для разработчика).
  2. Parity-тест red-suite (маркер ``pg``) сверяет ЭТОТ реестр ⇔ ``pg_policies`` на
     реальном PG — «добавил в реестр, забыл миграцию ENABLE RLS» → красный.

Процесс при новой tenant-таблице: (а) добавить сюда; (б) написать НОВУЮ миграцию
``ENABLE RLS`` + политики для неё (по образцу 0082). Оба теста ловят пропуск любого шага.

Миграция 0082 держит СВОЙ снапшот этих списков (историческая, самодостаточна — при
прогоне с нуля она создаёт политики для таблиц на момент 0082; будущие таблицы —
своими миграциями). Не импортируем реестр в 0082, иначе полный ре-прогон упал бы на
таблице, которой на момент 0082 ещё нет в схеме.
"""

from __future__ import annotations

#: tenant-таблицы (свой tenant_id): политика изоляции p_{t}_tenant (app) + p_{t}_maintenance.
TENANT_TABLES: frozenset[str] = frozenset({
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
    # денормлённые дети Ф3-a (0080):
    "recipe_ingredients", "menu_plan_items", "payment_order_items",
    # денормлённые Ф3-b (0081):
    "react_checkpoint", "react_checkpoint_write",
})

#: Корень: app видит только СВОЮ строку (SELECT), identity — INSERT.
ROOT_TABLES: frozenset[str] = frozenset({"tenants"})

#: Без ENABLE RLS — осознанные исключения (каждое с причиной).
NO_RLS_TABLES: frozenset[str] = frozenset({
    "subscription_plans",        # public-справочник тарифов, без tenant_id
    "audit_log",                 # admin: app-гранты отозваны Ф1 (0078)
    "admin_dashboard_snapshots",  # admin: app-гранты отозваны Ф1 (0078)
    "admin_alerts_seen",         # admin: app-гранты отозваны Ф1 (0078)
    "admin_sessions",            # admin-auth #305 (0077): серверные сессии по tg_id
    "admin_login_challenges",    # admin-auth #305 (0077): challenge-журнал
    "runtime_config",            # operational: глобальный конфиг
    "signup_attempts",           # operational: identity пишет журнал попыток без RLS
    "poller_offsets",            # operational: состояние поллеров
    "poller_heartbeats",         # operational: heartbeat поллеров
    # legacy plan-execute, owner-исключение. НЕ «заморожена» (устаревшее
    # обоснование, audit 2026-07-18 db-migrations #5): таблица живая —
    # пишется `runtime/planner/step_ledger.py`, читается recovery.py /
    # recovery_scanner.py / tool_runtime.py. Колонки tenant_id у неё НЕТ
    # вообще, поэтому tenant-политика невозможна конструктивно; после флипа
    # DSN на app-роль строки читаемы/писабельны кросс-тенантно (грант 0078).
    # Осознанный hardening-пробел: PII там нет (operation_id — opaque,
    # step_id структурный), легитимные пути ходят по своему execution_id.
    "step_execution_ledger",
})

#: Куда sreda_identity имеет INSERT-грант (Ф1, провижн-bundle) — нужна INSERT-политика.
IDENTITY_INSERT_TABLES: frozenset[str] = frozenset({
    "tenants", "workspaces", "users", "assistants", "tenant_features",
    "tenant_subscriptions",
})
