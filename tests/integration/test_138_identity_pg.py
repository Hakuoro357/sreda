"""#138 Ф5-5b red-suite: identity-роль на РЕАЛЬНОМ Postgres.

Доказывает механизм узкой identity-роли (приёмка #3 плана 138-rls-final):
- резолв ТОЛЬКО через DEFINER ``sreda_resolve_external_identity`` (v2, 0083):
  n=0 → пусто; n=1 → данные; n>1 → маска NULL + match_count (identity не узнаёт ЧЬИ);
- прямые SELECT/UPDATE на bundle-таблицы и журналы — permission denied, причём
  поверхность ошибки не зависит от содержимого (нельзя выведать данные ошибками);
- INSERT-скоуп провижна работает; счётчики абьюза — только агрегаты через DEFINER.

DSN через env (нет env → skip, как в test_138_rls_pg):
    SREDA_PG_TEST_DSN_OWNER=postgresql+psycopg://postgres:...@localhost:15434/sreda_test
    SREDA_PG_TEST_DSN_IDENTITY=postgresql+psycopg://sreda_identity:identity@...
БД должна быть смигрирована до 0083 (роли создаёт 0078, функции — 0083).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.pool import NullPool

_DSN_OWNER = os.environ.get("SREDA_PG_TEST_DSN_OWNER")
_DSN_IDENTITY = os.environ.get("SREDA_PG_TEST_DSN_IDENTITY")
_DSN_APP = os.environ.get("SREDA_PG_TEST_DSN_APP")
_DSN_MAINT = os.environ.get("SREDA_PG_TEST_DSN_MAINT")

if not (_DSN_OWNER and _DSN_IDENTITY and _DSN_APP and _DSN_MAINT):
    pytest.skip(
        "SREDA_PG_TEST_DSN_OWNER/_IDENTITY/_APP/_MAINT не заданы — identity "
        "red-suite гоняется только на реальном Postgres (см. docstring)",
        allow_module_level=True,
    )


def _set_tenant(conn, tenant_id: str) -> None:
    conn.execute(text("SELECT set_config('sreda.tenant_id', :t, false)"), {"t": tenant_id})

_SFX = uuid.uuid4().hex[:8]
T_A = f"tenant_idA_{_SFX}"
T_B = f"tenant_idB_{_SFX}"
HASH_KNOWN = f"hash_known_{_SFX}"
MAX_AMBIG = f"max_ambig_{_SFX}"


@pytest.fixture(scope="module")
def engines():
    owner = create_engine(_DSN_OWNER, poolclass=NullPool)
    identity = create_engine(_DSN_IDENTITY, poolclass=NullPool)
    app = create_engine(_DSN_APP, poolclass=NullPool)
    maint = create_engine(_DSN_MAINT, poolclass=NullPool)
    with owner.begin() as c:
        for tid, hsh, mx in ((T_A, HASH_KNOWN, MAX_AMBIG), (T_B, None, MAX_AMBIG)):
            c.execute(text(
                "INSERT INTO tenants (id, name, created_at, approved_at) "
                "VALUES (:t, :t, now(), now()) ON CONFLICT (id) DO NOTHING"), {"t": tid})
            c.execute(text(
                "INSERT INTO users (id, tenant_id, tg_account_hash, max_account_id) "
                "VALUES (:u, :t, :h, :m) ON CONFLICT (id) DO NOTHING"),
                {"u": f"user_{tid}", "t": tid, "h": hsh, "m": mx})
    yield {"owner": owner, "identity": identity, "app": app, "maint": maint}
    with owner.begin() as c:
        c.execute(text(
            "DELETE FROM signup_attempts WHERE source_id_hash LIKE :p"), {"p": f"%{_SFX}%"})
        # bundle-чистка: сначала дети (workspaces/assistants/features/subs создаются в тестах)
        for tbl in ("tenant_subscriptions", "tenant_features", "assistants",
                    "workspaces", "users"):
            c.execute(text(f"DELETE FROM {tbl} WHERE tenant_id IN (:a, :b)"),
                      {"a": T_A, "b": T_B})
        c.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": T_A, "b": T_B})
    owner.dispose()
    identity.dispose()
    app.dispose()
    maint.dispose()


def _resolve(conn, channel: str, key: str):
    return conn.execute(text(
        "SELECT tenant_id, user_id, approved_at, tenant_deleted_at, match_count "
        "FROM sreda_resolve_external_identity(:c, :k)"), {"c": channel, "k": key}).all()


# ── резолв через DEFINER ────────────────────────────────────────────────────

def test_resolve_unknown_returns_empty(engines):
    with engines["identity"].connect() as c:
        assert _resolve(c, "telegram", "no_such_hash") == []


def test_resolve_known_single_returns_identity(engines):
    with engines["identity"].connect() as c:
        rows = _resolve(c, "telegram", HASH_KNOWN)
    assert len(rows) == 1
    tid, uid, approved, deleted, n = rows[0]
    assert (tid, uid, n) == (T_A, f"user_{T_A}", 1)
    assert approved is not None and deleted is None


def test_resolve_ambiguous_masks_identities(engines):
    """n>1: match_count честный, но ЧЬИ строки — не раскрывается (приёмка #3)."""
    with engines["identity"].connect() as c:
        rows = _resolve(c, "max", MAX_AMBIG)
    assert len(rows) == 1
    tid, uid, approved, deleted, n = rows[0]
    assert n == 2
    assert tid is None and uid is None and approved is None and deleted is None


# ── прямой доступ запрещён, поверхность ошибки однородна ────────────────────

def test_direct_select_denied_uniform_error(engines):
    """SELECT на bundle/журналы = 42501 всегда — и для существующих данных,
    и для пустых фильтров: ошибками содержимое не выведать."""
    states = set()
    with engines["identity"].connect() as c:
        for tbl, flt in (
            ("users", f"tg_account_hash = '{HASH_KNOWN}'"),   # данные есть
            ("users", "tg_account_hash = 'no_such'"),          # данных нет
            ("tenants", "1=1"),
            ("signup_attempts", "1=1"),
            ("tenant_subscriptions", "1=1"),
            ("workspaces", "1=1"),
        ):
            with pytest.raises(ProgrammingError) as ei:
                c.execute(text(f"SELECT * FROM {tbl} WHERE {flt}"))
            states.add(ei.value.orig.sqlstate)
            c.rollback()
    assert states == {"42501"}  # insufficient_privilege, без вариаций


def test_updates_denied(engines):
    with engines["identity"].connect() as c:
        for stmt in (
            f"UPDATE users SET last_bot_key = 'x' WHERE tenant_id = '{T_A}'",
            f"UPDATE tenants SET approved_at = now() WHERE id = '{T_A}'",
        ):
            with pytest.raises(ProgrammingError) as ei:
                c.execute(text(stmt))
            assert ei.value.orig.sqlstate == "42501"
            c.rollback()


# ── INSERT-скоуп провижна ───────────────────────────────────────────────────

def test_insert_bundle_and_attempt_allowed(engines):
    """Провижн — PLAIN INSERT (без ON CONFLICT: у identity нет SELECT для
    arbiter-проверки — см. test_on_conflict_denied). Свежий тенант, конфликтов нет."""
    tid = f"tenant_new_{_SFX}"
    with engines["identity"].begin() as c:
        c.execute(text(
            "INSERT INTO tenants (id, name, created_at, approved_at) "
            "VALUES (:t, :t, now(), now())"), {"t": tid})
        c.execute(text(
            "INSERT INTO workspaces (id, tenant_id, name) VALUES (:w, :t, 'w')"),
            {"w": f"ws_{tid}", "t": tid})
        c.execute(text(
            "INSERT INTO users (id, tenant_id, tg_account_hash) VALUES (:u, :t, :h)"),
            {"u": f"user_{tid}", "t": tid, "h": f"hash_new_{_SFX}"})
        c.execute(text(
            "INSERT INTO assistants (id, tenant_id, workspace_id, name) "
            "VALUES (:a, :t, :w, 'Среда')"),
            {"a": f"as_{tid}", "t": tid, "w": f"ws_{tid}"})
        c.execute(text(
            "INSERT INTO tenant_features (id, tenant_id, feature_key, enabled) "
            "VALUES (:i, :t, 'core_assistant', true)"),
            {"i": f"{tid}:core_assistant", "t": tid})
        c.execute(text(
            "INSERT INTO signup_attempts (channel, source_id_hash, attempted_at) "
            "VALUES ('telegram', :h, now())"), {"h": f"attempt_{_SFX}"})
    # видимость — под owner (identity сам прочитать не может, и это правильно)
    with engines["owner"].connect() as c:
        n = c.execute(text("SELECT count(*) FROM users WHERE tenant_id = :t"),
                      {"t": tid}).scalar()
    assert n == 1
    with engines["owner"].begin() as c:  # cleanup
        for tbl in ("tenant_features", "assistants", "users", "workspaces"):
            c.execute(text(f"DELETE FROM {tbl} WHERE tenant_id = :t"), {"t": tid})
        c.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})


def test_on_conflict_denied_idempotency_via_integrity_error(engines):
    """КЛЮЧЕВАЯ находка red-suite: identity НЕ может ON CONFLICT (arbiter-проверка
    требует SELECT-привилегию → permission denied). Поэтому идемпотентность
    провижна — plain INSERT + перехват unique_violation, а не ON CONFLICT."""
    # ON CONFLICT под identity = permission denied (42501), НЕ тихий no-op:
    with engines["identity"].connect() as c:
        with pytest.raises(ProgrammingError) as ei:
            c.execute(text(
                "INSERT INTO tenants (id, name, created_at) "
                "VALUES (:t, :t, now()) ON CONFLICT (id) DO NOTHING"), {"t": T_A})
        assert ei.value.orig.sqlstate == "42501"
        c.rollback()
    # Механизм идемпотентности: plain INSERT дубля → unique_violation (23505),
    # который вызывающий гасит savepoint'ом. НЕ permission denied.
    from sqlalchemy.exc import IntegrityError
    with engines["identity"].connect() as c:
        with pytest.raises(IntegrityError) as ei:
            c.execute(text(
                "INSERT INTO tenants (id, name, created_at) "
                "VALUES (:t, :t, now())"), {"t": T_A})
        assert ei.value.orig.sqlstate == "23505"
        c.rollback()


# ── счётчики абьюза: агрегаты без чтения строк ──────────────────────────────

def test_counter_functions_work_under_identity(engines):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    with engines["identity"].begin() as c:
        base = c.execute(text(
            "SELECT sreda_signup_recent_attempts_count('telegram', :h, :cut)"),
            {"h": f"cnt_{_SFX}", "cut": cutoff}).scalar()
        c.execute(text(
            "INSERT INTO signup_attempts (channel, source_id_hash, attempted_at) "
            "VALUES ('telegram', :h, now())"), {"h": f"cnt_{_SFX}"})
        after = c.execute(text(
            "SELECT sreda_signup_recent_attempts_count('telegram', :h, :cut)"),
            {"h": f"cnt_{_SFX}", "cut": cutoff}).scalar()
        cap = c.execute(text("SELECT sreda_signup_free_tier_active_count()")).scalar()
    assert (base, after) == (0, 1)
    assert isinstance(cap, int) and cap >= 0


def test_setconfig_reemitted_per_transaction(engines):
    """#138 Ф5-5c (Codex R4 MINOR): КЛЮЧЕВОЕ свойство обёртки inbound-хвоста —
    begin-событие app-движка ре-эмитит set_config('sreda.tenant_id') на КАЖДОЙ
    транзакции пока tenant_ctx выставлен (is_local=true → сбрасывается на commit,
    ре-эмитится на следующем begin). Именно это делает persist_* (внутренний commit)
    + последующие entitlement/status ре-скоупленными. Ломается — весь inbound после
    флипа умрёт. Тестируем реальный механизм db/session._emit_tenant_setconfig."""
    from sqlalchemy import event

    from sreda.db.session import _emit_tenant_setconfig, tenant_ctx

    app = create_engine(_DSN_APP, poolclass=NullPool)
    event.listen(app, "begin", _emit_tenant_setconfig)
    tok = tenant_ctx.set(T_A)
    try:
        with app.connect() as c:
            with c.begin():  # txn1
                v1 = c.execute(
                    text("SELECT current_setting('sreda.tenant_id', true)")
                ).scalar()
            with c.begin():  # txn2 — после commit txn1, ре-эмит на новом begin
                v2 = c.execute(
                    text("SELECT current_setting('sreda.tenant_id', true)")
                ).scalar()
        assert v1 == T_A, "GUC не выставлен в первой транзакции"
        assert v2 == T_A, "GUC НЕ ре-эмитился после commit — inbound-хвост сломался бы"
    finally:
        tenant_ctx.reset(tok)
        app.dispose()
    # без ctx — GUC пуст (fail-closed):
    app2 = create_engine(_DSN_APP, poolclass=NullPool)
    event.listen(app2, "begin", _emit_tenant_setconfig)
    try:
        with app2.connect() as c, c.begin():
            v = c.execute(text("SELECT current_setting('sreda.tenant_id', true)")).scalar()
        assert v in (None, ""), "без tenant_ctx GUC должен быть пуст"
    finally:
        app2.dispose()


def test_real_engine_scopes_handler_sequence(engines, monkeypatch):
    """#353: усиление re-emit-теста (Codex high R5 MINOR). Прошлый тест вешал
    слушатель вручную → прошёл бы, даже если регистрацию на get_engine() убрать.
    Здесь — РЕАЛЬНЫЙ путь: env DSN → get_engine() (сам регистрирует begin-слушатель)
    → get_session_factory → handler-последовательность: операция под ctx →
    ВНУТРЕННИЙ commit (как persist_*) → следующий стейтмент ре-скоуплен →
    reset ctx → fail-closed 0 строк."""
    from sreda.config.settings import get_settings
    from sreda.db import session as dbs

    monkeypatch.setenv("SREDA_DATABASE_URL", _DSN_APP)
    get_settings.cache_clear()
    dbs.get_engine.cache_clear()
    dbs.get_session_factory.cache_clear()
    dbs._factory_for.cache_clear()
    dbs._build_role_engine.cache_clear()
    try:
        tok = dbs.tenant_ctx.set(T_A)
        try:
            s = dbs.get_session_factory()()
            try:
                # txn1: своя строка users видна и обновляется (p_users_tenant)
                n = s.execute(text(
                    "UPDATE users SET last_bot_key='doors_test' WHERE tenant_id=:t"
                ), {"t": T_A}).rowcount
                assert n == 1, "под ctx своя строка должна обновляться"
                s.commit()  # внутренний commit — как в persist_*
                # txn2 (после commit): begin-слушатель обязан РЕ-эмитить set_config
                seen = s.execute(text(
                    "SELECT count(*) FROM users WHERE tenant_id = :t"
                ), {"t": T_A}).scalar()
                assert seen == 1, (
                    "GUC не ре-эмитился после внутреннего commit — "
                    "handler-хвост сломался бы после флипа"
                )
                s.commit()
            finally:
                s.close()
        finally:
            dbs.tenant_ctx.reset(tok)
        # после reset — fail-closed: без ctx 0 строк
        s2 = dbs.get_session_factory()()
        try:
            assert s2.execute(text("SELECT count(*) FROM users")).scalar() == 0, (
                "без tenant_ctx app-роль должна видеть 0 строк (fail-closed)"
            )
        finally:
            s2.close()
    finally:
        # вернуть глобальные кеши в исходное (не отравить другие тесты)
        get_settings.cache_clear()
        dbs.get_engine.cache_clear()
        dbs.get_session_factory.cache_clear()
        dbs._factory_for.cache_clear()
        dbs._build_role_engine.cache_clear()


def test_existing_user_path_roles(engines):
    """#138 Ф5-5c: операции обслуживания СУЩЕСТВУЮЩЕГО юзера проходят под нужными
    ролями (после флипа DSN). app+ctx: UPDATE users own (last_bot_key). maintenance
    (privileged репаир): UPDATE tenants.approved_at + INSERT tenant_subscriptions.
    Иначе existing-юзер сломался бы на флипе (тема, что нашли high+субагент)."""
    # T_A/user засеяны фикстурой. app под ctx=A обновляет свою строку users:
    with engines["app"].begin() as c:
        _set_tenant(c, T_A)
        c.execute(text("UPDATE users SET last_bot_key='sreda' WHERE tenant_id=:t"),
                  {"t": T_A})
    # app под ctx=A НЕ видит/не трогает чужого B:
    with engines["app"].begin() as c:
        _set_tenant(c, T_A)
        r = c.execute(text("UPDATE users SET last_bot_key='x' WHERE tenant_id=:t"),
                      {"t": T_B})
        assert r.rowcount == 0  # чужой невидим под своим ctx
    # maintenance (репаир pending): UPDATE tenants + прямой SELECT subscription_plans
    # (maintenard имеет грант; _auto_approve идёт так, НЕ через identity-DEFINER) +
    # INSERT подписки:
    with engines["maint"].begin() as c:
        c.execute(text("UPDATE tenants SET approved_at=now() WHERE id=:t"), {"t": T_A})
        plan = c.execute(text(
            "SELECT id, feature_key FROM subscription_plans WHERE plan_key='sreda_free' LIMIT 1"
        )).first()
        if plan is not None:
            c.execute(text(
                "INSERT INTO tenant_subscriptions "
                "(id, tenant_id, plan_id, feature_key, status, starts_at, "
                " cancel_at_period_end, quantity, next_cycle_quantity, created_at, updated_at) "
                "VALUES (:i, :t, :p, :f, 'active', now(), false, 1, 1, now(), now())"),
                {"i": f"sub_5c_{_SFX}", "t": T_A, "p": plan[0], "f": plan[1]})
    with engines["owner"].begin() as c:  # cleanup sub
        c.execute(text("DELETE FROM tenant_subscriptions WHERE id=:i"), {"i": f"sub_5c_{_SFX}"})


def test_free_plan_via_definer_not_direct_select(engines):
    """R2 (Codex high MAJOR-6): identity читает тариф ТОЛЬКО через DEFINER
    sreda_free_plan() — прямой SELECT на subscription_plans запрещён (роль без
    SELECT-гранта даже на публичный справочник)."""
    with engines["identity"].connect() as c:
        # прямой SELECT — permission denied
        with pytest.raises(ProgrammingError) as ei:
            c.execute(text("SELECT id, plan_key FROM subscription_plans LIMIT 1"))
        assert ei.value.orig.sqlstate == "42501"
        c.rollback()
        # DEFINER-функция — работает, возвращает только id+feature_key
        rows = c.execute(text("SELECT plan_id, feature_key FROM sreda_free_plan()")).all()
    assert len(rows) <= 1  # 0 если тариф не засеян, 1 если есть
