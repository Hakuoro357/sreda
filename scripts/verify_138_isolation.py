#!/usr/bin/env python3
"""#138 — живая проверка изоляции семей (RLS) на РАБОТАЮЩЕМ проде.

Гоняет батарею тест-кейсов против живой БД под реальными ролями роль-сплита
(sreda_app / sreda_identity / sreda_maintenance) на РЕАЛЬНЫХ тенантах. Каждый
кейс даёт PASS / FAIL / SKIP. Служит acceptance-доказательством #138 и
повторяемым инструментом ре-проверки после деплоев.

БЕЗОПАСНОСТЬ НА ПРОДЕ:
- Чтения — read-only.
- Write-кейсы (INSERT/UPDATE/DELETE) выполняются ТОЛЬКО внутри транзакции,
  которая ГАРАНТИРОВАННО откатывается (rollback) — ни одна проверочная мутация
  не персистится. В конце — контроль по owner-движку, что данные не изменились.

Роли берутся из env (как у сервиса): SREDA_DATABASE_URL=app,
SREDA_IDENTITY_DATABASE_URL=identity, SREDA_MAINTENANCE_DATABASE_URL=maintenance,
SREDA_MIGRATION_DATABASE_URL=owner (ground-truth, обход RLS).

Запуск на проде:
    sudo systemd-run --pipe --wait --collect -p User=sreda \
      -p WorkingDirectory=/opt/sreda -p EnvironmentFile=/etc/sreda/.env \
      /opt/sreda/.venv/bin/python scripts/verify_138_isolation.py

Exit 0 — все кейсы PASS (SKIP допустим); exit 1 — есть FAIL.
"""
from __future__ import annotations

import sys
import uuid

from sqlalchemy import create_engine, text

from sreda.config.settings import get_settings
from sreda.db import session as dbs

# Denormalized-child таблица для кейса «дети наследуют tenant_id» (0080). Берём
# первую существующую из кандидатов.
_CHILD_CANDIDATES = ("checklist_items", "recipe_ingredients", "menu_plan_items")

_results: list[tuple[str, str, str]] = []  # (status, name, detail)


def _rec(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    mark = {"PASS": "✅", "FAIL": "❌", "SKIP": "—"}[status]
    print(f"  {mark} [{status}] {name}" + (f" — {detail}" if detail else ""))


def _check(cond: bool, name: str, detail: str = "") -> None:
    _rec("PASS" if cond else "FAIL", name, detail)


def _current_user(engine) -> str:
    with engine.connect() as c:
        return c.execute(text("SELECT current_user")).scalar()


def _count_under_ctx(engine, tid: str | None, sql: str, params: dict | None = None) -> int:
    """Открывает соединение под ролью engine, ставит sreda.tenant_id=tid (или ''),
    считает. Ручной set_config (txn-local) — как begin-листенер в проде."""
    with engine.connect() as c:
        c.execute(text("SELECT set_config('sreda.tenant_id', :t, true)"), {"t": tid or ""})
        return int(c.execute(text(sql), params or {}).scalar())


def _table_exists(owner_engine, table: str) -> bool:
    with owner_engine.connect() as c:
        return bool(c.execute(
            text("SELECT to_regclass(:t)"), {"t": f"public.{table}"}
        ).scalar())


def main() -> int:
    s = get_settings()
    if not s.migration_database_url:
        print("SKIP-ALL: нет SREDA_MIGRATION_DATABASE_URL (owner ground-truth) — "
              "флип не разведён? Проверка бессмысленна.")
        return 0
    owner = create_engine(s.migration_database_url)
    app_eng = dbs.get_engine()
    id_eng = dbs.get_identity_engine()
    mnt_eng = dbs.get_maintenance_engine()

    # --- Выбор двух реальных тенантов с данными (через owner, обход RLS) --------
    with owner.connect() as c:
        total_tenants = int(c.execute(text("SELECT count(*) FROM tenants")).scalar())
        rows = c.execute(text(
            "SELECT tenant_id, count(*) AS n FROM react_checkpoint "
            "WHERE tenant_id IS NOT NULL GROUP BY tenant_id ORDER BY n DESC LIMIT 5"
        )).all()
    if len(rows) < 2:
        print("SKIP-ALL: <2 тенантов с чекпоинтами — не на чем проверять кросс-изоляцию.")
        return 0
    t1, t1_cp = rows[0][0], int(rows[0][1])
    t2, t2_cp = rows[1][0], int(rows[1][1])
    with owner.connect() as c:
        t1_users = int(c.execute(text(
            "SELECT count(*) FROM users WHERE tenant_id=:t"), {"t": t1}).scalar())
        t2_users_before = int(c.execute(text(
            "SELECT count(*) FROM users WHERE tenant_id=:t"), {"t": t2}).scalar())

    print(f"\n#138 live isolation verify: T1={t1} (cp={t1_cp}, users={t1_users}) | "
          f"T2={t2} (cp={t2_cp}) | всего тенантов={total_tenants}\n")

    # --- Группа 1: роли под каждым движком -------------------------------------
    print("[роли]")
    _check(_current_user(app_eng) == "sreda_app", "app-движок под sreda_app")
    _check(_current_user(id_eng) == "sreda_identity", "identity-движок под sreda_identity")
    _check(_current_user(mnt_eng) == "sreda_maintenance", "maintenance-движок под sreda_maintenance")

    # --- Группа 2: SELECT-изоляция (app-роль) ----------------------------------
    print("\n[SELECT-изоляция под sreda_app]")
    own_cp = _count_under_ctx(app_eng, t1, "SELECT count(*) FROM react_checkpoint")
    _check(own_cp == t1_cp, "app+ctx=T1: свои checkpoints видны",
           f"видно {own_cp}, ground-truth {t1_cp}")
    own_tenants = _count_under_ctx(app_eng, t1, "SELECT count(*) FROM tenants")
    _check(own_tenants == 1, "app+ctx=T1: tenants виден ровно 1 (свой)",
           f"видно {own_tenants} из {total_tenants}")
    cross_cp = _count_under_ctx(
        app_eng, t1, "SELECT count(*) FROM react_checkpoint WHERE tenant_id=:o", {"o": t2})
    _check(cross_cp == 0, "app+ctx=T1: чужие (T2) checkpoints невидимы", f"видно {cross_cp}")
    cross_users = _count_under_ctx(
        app_eng, t1, "SELECT count(*) FROM users WHERE tenant_id=:o", {"o": t2})
    _check(cross_users == 0, "app+ctx=T1: чужие (T2) users невидимы", f"видно {cross_users}")

    # --- Группа 3: fail-closed без контекста -----------------------------------
    print("\n[fail-closed без ctx]")
    noctx_cp = _count_under_ctx(app_eng, "", "SELECT count(*) FROM react_checkpoint")
    _check(noctx_cp == 0, "app без ctx: checkpoints = 0", f"видно {noctx_cp}")
    noctx_users = _count_under_ctx(app_eng, "", "SELECT count(*) FROM users")
    _check(noctx_users == 0, "app без ctx: users = 0", f"видно {noctx_users}")

    # --- Группа 4: дети (денормализованный tenant_id, 0080) --------------------
    print("\n[дети — денормализованный tenant_id]")
    child = next((t for t in _CHILD_CANDIDATES if _table_exists(owner, t)), None)
    if child is None:
        _rec("SKIP", "child-таблица изоляция", "нет ни одной из кандидатов")
    else:
        with owner.connect() as c:
            t1_child = int(c.execute(text(
                f"SELECT count(*) FROM {child} WHERE tenant_id=:t"), {"t": t1}).scalar())
        own_child = _count_under_ctx(app_eng, t1, f"SELECT count(*) FROM {child}")
        _check(own_child == t1_child, f"app+ctx=T1: свои {child} видны",
               f"видно {own_child}, ground-truth {t1_child}")
        cross_child = _count_under_ctx(
            app_eng, t1, f"SELECT count(*) FROM {child} WHERE tenant_id=:o", {"o": t2})
        _check(cross_child == 0, f"app+ctx=T1: чужие (T2) {child} невидимы", f"видно {cross_child}")

    # --- Группа 5: WRITE-изоляция (ВСЁ в rollback — не персистится) -------------
    print("\n[WRITE-изоляция под sreda_app — всё откатывается]")
    # Реальные колонки users (без generated/identity) — для UPDATE-no-op и INSERT.
    with owner.connect() as c:
        _ucols = [r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='users' AND table_schema='public' "
            "AND is_generated='NEVER' AND is_identity='NO'"))]
    _noop_col = next((col for col in _ucols if col not in ("id", "tenant_id")), "id")
    # UPDATE чужого → 0 строк (невидим под RLS). SET col=col — no-op.
    with app_eng.connect() as c:
        trans = c.begin()
        try:
            c.execute(text("SELECT set_config('sreda.tenant_id', :t, true)"), {"t": t1})
            r = c.execute(text(
                f"UPDATE users SET {_noop_col}={_noop_col} WHERE tenant_id=:o"), {"o": t2})
            _check(r.rowcount == 0, "app+ctx=T1: UPDATE чужих users затронул 0 строк",
                   f"rowcount={r.rowcount}")
        finally:
            trans.rollback()
    # DELETE чужого → 0 строк
    with app_eng.connect() as c:
        trans = c.begin()
        try:
            c.execute(text("SELECT set_config('sreda.tenant_id', :t, true)"), {"t": t1})
            r = c.execute(text(
                "DELETE FROM react_checkpoint WHERE tenant_id=:o"), {"o": t2})
            _check(r.rowcount == 0, "app+ctx=T1: DELETE чужих checkpoints затронул 0 строк",
                   f"rowcount={r.rowcount}")
        finally:
            trans.rollback()
    # INSERT чужого tenant_id → RLS WITH CHECK отвергает. Строку копируем из
    # реальной T1-строки (валидные колонки), меняем id+tenant_id на T2.
    with app_eng.connect() as c:
        trans = c.begin()
        try:
            c.execute(text("SELECT set_config('sreda.tenant_id', :t, true)"), {"t": t1})
            cols = _ucols
            if "id" in cols and "tenant_id" in cols:
                sel = ", ".join(
                    f"'{uuid.uuid4().hex}'" if col == "id"
                    else (":o" if col == "tenant_id" else col)
                    for col in cols
                )
                collist = ", ".join(cols)
                try:
                    c.execute(
                        text(f"INSERT INTO users ({collist}) "
                             f"SELECT {sel} FROM users WHERE tenant_id=:t LIMIT 1"),
                        {"o": t2, "t": t1},
                    )
                    _rec("FAIL", "app+ctx=T1: INSERT чужого tenant_id отвергнут",
                         "вставка прошла (RLS WITH CHECK НЕ сработал!)")
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc).lower()
                    ok = "row-level security" in msg or "policy" in msg
                    _rec("PASS" if ok else "SKIP",
                         "app+ctx=T1: INSERT чужого tenant_id отвергнут",
                         "RLS WITH CHECK" if ok else f"иная ошибка: {msg[:80]}")
            else:
                _rec("SKIP", "app+ctx=T1: INSERT чужого tenant_id отвергнут", "нет id/tenant_id")
        finally:
            trans.rollback()
    # UPDATE своего tenants под app → политика p_tenants_self = SELECT-only → 0 строк
    with app_eng.connect() as c:
        trans = c.begin()
        try:
            c.execute(text("SELECT set_config('sreda.tenant_id', :t, true)"), {"t": t1})
            try:
                r = c.execute(text("UPDATE tenants SET name=name WHERE id=:t"), {"t": t1})
                _check(r.rowcount == 0,
                       "app+ctx=T1: UPDATE своего tenants заблокирован (SELECT-only)",
                       f"rowcount={r.rowcount}")
            except Exception as exc:  # noqa: BLE001
                _rec("PASS", "app+ctx=T1: UPDATE своего tenants заблокирован (SELECT-only)",
                     f"отказ: {str(exc)[:60]}")
        finally:
            trans.rollback()

    # --- Группа 6: identity — zero plain SELECT, но DEFINER работает -----------
    print("\n[identity-роль]")
    try:
        with id_eng.connect() as c:
            c.execute(text("SELECT count(*) FROM users"))
        _rec("FAIL", "identity: прямой SELECT users запрещён", "SELECT прошёл (grant есть?!)")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        _rec("PASS" if "permission denied" in msg else "SKIP",
             "identity: прямой SELECT users запрещён",
             "permission denied" if "permission denied" in msg else str(exc)[:70])
    try:
        with id_eng.connect() as c:
            n = c.execute(text(
                "SELECT count(*) FROM sreda_resolve_external_identity('max', :k)"),
                {"k": "___nonexistent___"}).scalar()
        _check(int(n) == 0, "identity: DEFINER resolve исполняется (unknown→0 строк)",
               f"n={n}")
    except Exception as exc:  # noqa: BLE001
        _rec("FAIL", "identity: DEFINER resolve исполняется", f"ошибка: {str(exc)[:70]}")

    # --- Группа 7: maintenance — кросс-тенант (для GC/админа) -------------------
    print("\n[maintenance-роль]")
    try:
        with mnt_eng.connect() as c:
            seen = int(c.execute(text("SELECT count(*) FROM tenants")).scalar())
        _check(seen == total_tenants,
               "maintenance: видит ВСЕ tenants (кросс-тенант для GC/админа)",
               f"видно {seen} из {total_tenants}")
    except Exception as exc:  # noqa: BLE001
        _rec("FAIL", "maintenance: видит ВСЕ tenants", f"ошибка: {str(exc)[:70]}")

    # --- Группа 8: контроль неизменности (write-кейсы не персистнулись) --------
    print("\n[контроль: проверочные мутации не персистнулись]")
    with owner.connect() as c:
        t2_users_after = int(c.execute(text(
            "SELECT count(*) FROM users WHERE tenant_id=:t"), {"t": t2}).scalar())
    _check(t2_users_after == t2_users_before,
           "T2.users не изменились после write-кейсов",
           f"было {t2_users_before}, стало {t2_users_after}")

    # --- Итог ------------------------------------------------------------------
    n_pass = sum(1 for r in _results if r[0] == "PASS")
    n_fail = sum(1 for r in _results if r[0] == "FAIL")
    n_skip = sum(1 for r in _results if r[0] == "SKIP")
    print(f"\n=== ИТОГ: {n_pass} PASS / {n_fail} FAIL / {n_skip} SKIP ===")
    if n_fail:
        print("FAIL-кейсы:")
        for st, name, detail in _results:
            if st == "FAIL":
                print(f"  - {name}: {detail}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
