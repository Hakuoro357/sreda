"""#138 Ф1: RLS роль-сплит — роли + гранты + resolve DEFINER (PG-only).

Аддитивно и ПРОД-БЕЗОПАСНО: создаёт роли `sreda_app`/`sreda_identity`/`sreda_maintenance`
(NOINHERIT, NOBYPASSRLS, LOGIN без пароля — ops проставит пароль + разведёт DSN на Ф5) +
пер-табличные гранты (по Ф0-переписи) + SECURITY DEFINER `sreda_resolve_external_identity`
(хардено: SET search_path, REVOKE PUBLIC, EXECUTE только identity).

Границы фазы:
- RLS ENABLE + именованные политики + денормализация детей — Ф3 (НЕ здесь; без политик роли
  ничего не энфорсят — это лишь предпосылка).
- Провижн-код на INSERT-ON-CONFLICT (без pre-SELECT под identity) — Ф2. До Ф5-флипа DSN
  identity == текущая роль sreda (имеет SELECT) → провижн не ломается.
- НЕ ставим ALTER DEFAULT PRIVILEGES (fail-open, R6) — будущие таблицы получают явный GRANT в
  своей миграции; мета-тест дрейфа Ф3 ловит пропуск.

SQLite (unit-тесты) — гард-skip (нет ролей/DEFINER/RLS). Верификация SQL: trio-ревью + Ф5
репетиция на дампе прода под ролями (локально PG нет).

Ф1-R1 (trio) применено: resolve fail-closed на неоднозначный max_account_id + возвращает deleted_at
(гейт) + хардинг search_path/квалификация; identity USAGE на sequences; убраны нерабочие UPDATE-гранты
(Ф2 через DEFINER/ON CONFLICT); to_regclass-гард REVOKE.
Ф2-заметки (не Ф1): identity нужен SELECT на signup_attempts для SignupAbuseGuard COUNT (или вынести
abuse-проверку из identity-транзакции); resolve INNER JOIN + fail-closed отличается от one_or_none —
провижн Ф2 учитывает.
"""
from alembic import op

revision = "20260706_0078"
down_revision = "20260704_0077"
branch_labels = None
depends_on = None


# ── Роли (идемпотентно; без пароля — ops задаёт ALTER ROLE ... PASSWORD вне репо) ──
_ROLES_SQL = """
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sreda_app') THEN
    CREATE ROLE sreda_app LOGIN NOINHERIT NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sreda_identity') THEN
    CREATE ROLE sreda_identity LOGIN NOINHERIT NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sreda_maintenance') THEN
    CREATE ROLE sreda_maintenance LOGIN NOINHERIT NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END
$$;
"""

_GRANTS_SQL = """
-- USAGE на схему + последовательности. app/maintenance — на все (широкие роли). identity УЗКО:
-- только на нужную ей seq (Ф1-R2 MAJOR: не расширять привилегии узкой роли). signup_attempts.id
-- autoincrement → INSERT без USAGE на seq падал бы (Ф1-R1). Guard to_regclass: no-op при IDENTITY
-- (owned, грант не нужен) / грант при SERIAL.
GRANT USAGE ON SCHEMA public TO sreda_app, sreda_identity, sreda_maintenance;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO sreda_app, sreda_maintenance;
DO $$
BEGIN
  IF to_regclass('public.signup_attempts_id_seq') IS NOT NULL THEN
    GRANT USAGE ON SEQUENCE public.signup_attempts_id_seq TO sreda_identity;
  END IF;
END
$$;

-- sreda_app (рантайм): DML на ВСЕ существующие таблицы, КРОМЕ global_admin.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO sreda_app;

-- sreda_maintenance (админ/GC/монитор): DML на всё (политики Ф3 пустят по current_user).
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO sreda_maintenance;

-- global_admin — только maintenance: снять у app. Гард to_regclass (Ф1-R1 MAJOR: REVOKE
-- отсутствующей таблицы аборт'ит миграцию — не падать при будущем переименовании/удалении).
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['audit_log','admin_dashboard_snapshots','admin_alerts_seen'] LOOP
    IF to_regclass('public.'||t) IS NOT NULL THEN
      EXECUTE format('REVOKE ALL ON public.%I FROM sreda_app', t);
    END IF;
  END LOOP;
END
$$;

-- sreda_identity (публичный inbound-провижн): УЗКО. ТОЛЬКО INSERT на bundle-таблицы + журнал попыток;
-- НЕТ широкого SELECT (нельзя перечислить чужое) — резолюция через DEFINER-функцию (ниже).
-- UPDATE существующих строк в Ф1 НЕ гранты (Ф1-R1 MAJOR: PG требует SELECT на predicate-колонки WHERE
-- → узкий UPDATE-грант без SELECT нерабочий). Все мутации СУЩЕСТВУЮЩИХ строк под identity в Ф2 идут
-- через SECURITY DEFINER-функции / INSERT-ON-CONFLICT (без predicate-SELECT). До Ф5 identity==sreda.
GRANT INSERT ON tenants, workspaces, users, assistants, tenant_features, tenant_subscriptions,
  signup_attempts TO sreda_identity;
"""

# ── SECURITY DEFINER resolve: канал+ключ → tenant/user (+approved_at/deleted_at для гейтов). Хардено. ──
# Ф1-R1: (CRIT) fail-closed на НЕОДНОЗНАЧНОСТЬ — max_account_id НЕ уникален → строка ТОЛЬКО при ровно 1
# совпадении (n=1), иначе пусто (не привязать к чужой семье произвольным LIMIT 1). (MAJOR) вернуть
# deleted_at (soft-delete, core.py:40) — гейт is_tenant_active Python-стороной. (MAJOR) хардинг:
# search_path=pg_catalog + полностью квалифицированные public.users/public.tenants (не полагаться на
# writable public в search_path). Даёт identity РОВНО lookup, без прямого SELECT на users/tenants.
_RESOLVE_FN_SQL = """
CREATE OR REPLACE FUNCTION sreda_resolve_external_identity(p_channel text, p_key text)
RETURNS TABLE(tenant_id text, user_id text, approved_at timestamptz, tenant_deleted_at timestamptz)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
  SELECT q.tenant_id, q.user_id, q.approved_at, q.tenant_deleted_at
  FROM (
    SELECT u.tenant_id::text AS tenant_id, u.id::text AS user_id,
           t.approved_at AS approved_at, t.deleted_at AS tenant_deleted_at,
           count(*) OVER () AS n
    FROM public.users u
    JOIN public.tenants t ON t.id = u.tenant_id
    WHERE (p_channel = 'telegram' AND u.tg_account_hash = p_key)
       OR (p_channel = 'max'      AND u.max_account_id  = p_key)
  ) q
  WHERE q.n = 1
$$;
REVOKE ALL ON FUNCTION sreda_resolve_external_identity(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sreda_resolve_external_identity(text, text) TO sreda_identity;
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # роли/DEFINER/гранты — только Postgres; SQLite unit-DB не поддерживает
    op.execute(_ROLES_SQL)
    op.execute(_GRANTS_SQL)
    op.execute(_RESOLVE_FN_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP FUNCTION IF EXISTS sreda_resolve_external_identity(text, text);")
    # Гранты снимаем; роли НЕ дропаем авто (могут владеть объектами/сессиями) — ops вручную.
    op.execute("""
    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM sreda_app, sreda_identity, sreda_maintenance;
    -- sreda_identity ТОЖЕ (Ф1-R2 MINOR: симметрия отката — узкий sequence-грант identity снять).
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM sreda_app, sreda_maintenance, sreda_identity;
    REVOKE USAGE ON SCHEMA public FROM sreda_app, sreda_identity, sreda_maintenance;
    """)
