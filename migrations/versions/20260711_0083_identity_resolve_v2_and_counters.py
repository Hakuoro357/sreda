"""#138 Ф5-5b: resolve v2 (match_count) + DEFINER-счётчики signup-abuse + грант тарифов.

Зачем (PG-only, аддитивно, инертно до флипа DSN):
1. ``sreda_resolve_external_identity`` v2 — добавлен ``match_count``. Семантика v1
   (fail-closed пусто при n<>1) неотличима для вызывающего от «юзера нет» → тупая
   подмена SELECT на функцию создала бы ДУБЛЬ семьи для неоднозначного max_account_id
   (сейчас код отвечает отказом). v2: n=0 → пусто; n=1 → строка с данными; n>1 →
   строка (NULL, NULL, NULL, NULL, n) — вызывающий отказывает, но identity НЕ узнаёт
   ЧЬИ это строки (приёмка #3 «не перечисляет чужое»). Возвратный тип меняется →
   DROP + CREATE (CREATE OR REPLACE не умеет менять OUT-колонки). Код v1 не вызывал.
2. Два DEFINER-счётчика для SignupAbuseGuard (COUNT'ы под identity-ролью без
   SELECT-грантов на signup_attempts / tenant_subscriptions — identity по-прежнему
   не может перечислять содержимое):
   - ``sreda_signup_free_tier_active_count()`` — committed-cap (зеркало запроса
     signup_abuse.py: plan_key='sreda_free', active, не grandfathered);
   - ``sreda_signup_recent_attempts_count(channel, hash, cutoff)`` — rate-limit.
3. DEFINER ``sreda_free_plan()`` → (plan_id, feature_key) для sreda_free
   (R2 Codex high MAJOR-6 / medium CRIT-1: НЕ GRANT SELECT на весь справочник —
   держим identity без прямого SELECT даже на публичную таблицу; провижну нужен
   только plan_id/feature_key одного тарифа для INSERT tenant_subscriptions).

Харденинг всех функций — как у v1 (Ф1-R1): SECURITY DEFINER + SET search_path =
pg_catalog + полностью квалифицированные имена + REVOKE PUBLIC + EXECUTE только
sreda_identity.

Restartability (p-004): все шаги — CREATE OR REPLACE / DROP IF EXISTS / GRANT
(идемпотентны), autocommit-блоков нет — обычная транзакционная миграция.
"""

from alembic import op

revision = "20260711_0083"
down_revision = "20260709_0082"
branch_labels = None
depends_on = None

_UPGRADE_SQL = """
DROP FUNCTION IF EXISTS sreda_resolve_external_identity(text, text);
CREATE FUNCTION sreda_resolve_external_identity(p_channel text, p_key text)
RETURNS TABLE(tenant_id text, user_id text, approved_at timestamptz,
              tenant_deleted_at timestamptz, match_count bigint)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
  SELECT CASE WHEN q.n = 1 THEN q.tenant_id END,
         CASE WHEN q.n = 1 THEN q.user_id END,
         CASE WHEN q.n = 1 THEN q.approved_at END,
         CASE WHEN q.n = 1 THEN q.tenant_deleted_at END,
         q.n
  FROM (
    SELECT u.tenant_id::text AS tenant_id, u.id::text AS user_id,
           t.approved_at AS approved_at, t.deleted_at AS tenant_deleted_at,
           count(*) OVER () AS n
    FROM public.users u
    JOIN public.tenants t ON t.id = u.tenant_id
    WHERE (p_channel = 'telegram' AND u.tg_account_hash = p_key)
       OR (p_channel = 'max'      AND u.max_account_id  = p_key)
  ) q
  LIMIT 1
$$;
REVOKE ALL ON FUNCTION sreda_resolve_external_identity(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sreda_resolve_external_identity(text, text) TO sreda_identity;

CREATE OR REPLACE FUNCTION sreda_signup_free_tier_active_count()
RETURNS bigint
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
  SELECT count(*)
  FROM public.tenant_subscriptions ts
  JOIN public.subscription_plans sp ON ts.plan_id = sp.id
  WHERE sp.plan_key = 'sreda_free'
    AND ts.status = 'active'
    AND ts.grandfathered_at IS NULL
$$;
REVOKE ALL ON FUNCTION sreda_signup_free_tier_active_count() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sreda_signup_free_tier_active_count() TO sreda_identity;

CREATE OR REPLACE FUNCTION sreda_signup_recent_attempts_count(
    p_channel text, p_hash text, p_cutoff timestamptz)
RETURNS bigint
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
  SELECT count(*)
  FROM public.signup_attempts a
  WHERE a.channel = p_channel
    AND a.source_id_hash = p_hash
    AND a.attempted_at > p_cutoff
$$;
REVOKE ALL ON FUNCTION sreda_signup_recent_attempts_count(text, text, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sreda_signup_recent_attempts_count(text, text, timestamptz) TO sreda_identity;

CREATE OR REPLACE FUNCTION sreda_free_plan()
RETURNS TABLE(plan_id text, feature_key text)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
  SELECT sp.id::text, sp.feature_key
  FROM public.subscription_plans sp
  WHERE sp.plan_key = 'sreda_free'
  LIMIT 1
$$;
REVOKE ALL ON FUNCTION sreda_free_plan() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION sreda_free_plan() TO sreda_identity;
"""

# Откат: вернуть v1-форму функции (как в 0078), счётчики и грант снять.
_DOWNGRADE_SQL = """
DROP FUNCTION IF EXISTS sreda_resolve_external_identity(text, text);
CREATE FUNCTION sreda_resolve_external_identity(p_channel text, p_key text)
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

DROP FUNCTION IF EXISTS sreda_signup_free_tier_active_count();
DROP FUNCTION IF EXISTS sreda_signup_recent_attempts_count(text, text, timestamptz);
DROP FUNCTION IF EXISTS sreda_free_plan();
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # DEFINER/роли — только Postgres; SQLite unit-DB не поддерживает
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(_DOWNGRADE_SQL)
