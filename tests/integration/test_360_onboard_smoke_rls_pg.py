"""vex#360: onboard_smoke под RLS (#138) на РЕАЛЬНОМ Postgres.

После флипа DSN рантайм = sreda_app; слепая app-сессия без tenant-контекста видит
0 строк. До фикса верификация/маркировка/чистка smoke шли через голый
get_engine()/get_session_factory() → ложный ABORT + течь synthetic-тенантов на
каждый safe_restart. Здесь — red-suite на функции скрипта И сквозной прогон
``_run_smoke_locked`` со стабами inbound-пути: под роль-DSN'ами (app + maintenance)
скрипт обязан видеть, помечать и удалять синтетический тенант; safety-отказы
(неточный маркер / чужая активность / слепой maintenance) — сохраниться.

Ассерты — на ИСХОД владельцем (строки реально удалены/целы, exit-код), не на
текст/вайринг (g-057). Mutation-проверки против кода С фиксом: возврат верификации
_run_smoke_locked на голую get_session_factory → rc=1 (красный e2e-тест); возврат
маркировки/чистки на голый get_engine → тесты mark/cleanup красные (0 строк под
RLS); снятие sight-гейта → красный test_blind_maintenance_fail_closed. (Код ДО
фикса на этих тестах падает тривиально — у старых функций другие сигнатуры.)

DSN через env (нет env → skip, как в test_138_rls_pg):
    SREDA_PG_TEST_DSN_OWNER / SREDA_PG_TEST_DSN_APP / SREDA_PG_TEST_DSN_MAINT
БД смигрирована до >=0083, LOGIN-роли как в docstring test_138_rls_pg.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.pg

_DSN_OWNER = os.environ.get("SREDA_PG_TEST_DSN_OWNER")
_DSN_APP = os.environ.get("SREDA_PG_TEST_DSN_APP")
_DSN_MAINT = os.environ.get("SREDA_PG_TEST_DSN_MAINT")

if not (_DSN_OWNER and _DSN_APP and _DSN_MAINT):
    pytest.skip(
        "SREDA_PG_TEST_DSN_OWNER/_APP/_MAINT не заданы — smoke-RLS red-suite "
        "бежит только против реального Postgres (vex#360)",
        allow_module_level=True,
    )

# Синтетические chat'ы вне прод-значений smoke (-8999990001/-8999990002),
# тот же namespace (отрицательные, ~-9e9).
CHAT = -8_999_990_777        # функции-тесты
CHAT_E2E = -8_999_990_778    # сквозной _run_smoke_locked
TID = f"tenant_tg_{CHAT}"
TID_E2E = f"tenant_tg_{CHAT_E2E}"
PLAN_ID = "plan-360-test"

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "onboard_smoke.py"
_spec = importlib.util.spec_from_file_location("onboard_smoke_360", _SCRIPT)
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


@pytest.fixture(scope="module")
def owner():
    eng = create_engine(_DSN_OWNER, poolclass=NullPool)
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO subscription_plans (id, plan_key, feature_key, title, description, "
            "price_rub, created_at, updated_at) "
            "VALUES (:i, :i, 'smoke360', 't', 'd', 0, now(), now()) ON CONFLICT (id) DO NOTHING"),
            {"i": PLAN_ID})
    yield eng
    for tid, chat in ((TID, CHAT), (TID_E2E, CHAT_E2E)):
        try:
            _wipe(eng, tid, chat)
        except RuntimeError:  # хеш не кеширован (тесты чата не бежали) → и сеять было некому
            pass
    with eng.begin() as c:
        c.execute(text("DELETE FROM subscription_plans WHERE id = :i"), {"i": PLAN_ID})
    eng.dispose()


def _clear_engine_caches() -> None:
    from sreda.config.settings import get_settings
    from sreda.db import session as dbs

    # dispose ДО сброса кешей — иначе pooled-соединения висят до GC (sol R1 MINOR)
    try:
        dbs.get_engine().dispose()
        dbs.get_maintenance_engine().dispose()
    except Exception:  # noqa: BLE001 — движок мог не собраться (кривой env в negative-тестах)
        pass
    get_settings.cache_clear()
    dbs.get_engine.cache_clear()
    dbs.get_session_factory.cache_clear()
    dbs._factory_for.cache_clear()
    dbs._build_role_engine.cache_clear()


@pytest.fixture()
def role_env(monkeypatch):
    """Скрипт-функции ходят через sreda.db.session → направляем DSN'ы на роли
    и сбрасываем кеши движков (паттерн test_real_engine_scopes_handler_sequence)."""
    monkeypatch.setenv("SREDA_DATABASE_URL", _DSN_APP)
    monkeypatch.setenv("SREDA_MAINTENANCE_DATABASE_URL", _DSN_MAINT)
    _clear_engine_caches()
    yield
    _clear_engine_caches()


@pytest.fixture()
def blind_env(monkeypatch):
    """Полу-флип: app-DSN под RLS, maintenance-DSN НЕ разведён → privileged-фолбэк
    на слепой app-движок (класс CRITICAL адверс-ревью R1)."""
    monkeypatch.setenv("SREDA_DATABASE_URL", _DSN_APP)
    monkeypatch.delenv("SREDA_MAINTENANCE_DATABASE_URL", raising=False)
    _clear_engine_caches()
    yield
    _clear_engine_caches()


_HASH_CACHE: dict[int, str] = {}


def _signup_hash(chat: int) -> str:
    """hmac требует SREDA_TG_ACCOUNT_SALT из function-scoped conftest-фикстуры — к moment'у
    module-teardown env уже снят. Кешируем при первом вычислении (внутри тестов)."""
    if chat not in _HASH_CACHE:
        from sreda.services.signup_abuse import hmac_signup_source
        _HASH_CACHE[chat] = hmac_signup_source(str(chat))
    return _HASH_CACHE[chat]


def _wipe(owner_eng, tid: str, chat: int) -> None:
    """Идемпотентная зачистка сида владельцем (FK-порядок: дети → tenants)."""
    with owner_eng.begin() as c:
        for tbl in ("react_turn_trace", "tenant_subscriptions", "inbound_messages",
                    "users", "workspaces"):
            c.execute(text(f"DELETE FROM {tbl} WHERE tenant_id = :t"), {"t": tid})  # noqa: S608
        c.execute(
            text("DELETE FROM signup_attempts WHERE channel = 'telegram' AND source_id_hash = :h"),
            {"h": _signup_hash(chat)},
        )
        c.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": tid})


def _seed(owner_eng, *, name: str, update_id: str, deep: bool = True) -> None:
    """Пересев владельцем: tenants + inbound (наш/чужой update_id) + signup-строка;
    deep — плюс FK-граф (workspaces + users + подписка + трейс), чтобы чистка
    реально проходила многослойный граф, а refuse-тесты проверяли сохранность детей."""
    _wipe(owner_eng, TID, CHAT)
    now = datetime.now(timezone.utc)
    with owner_eng.begin() as c:
        c.execute(
            text("INSERT INTO tenants (id, name, created_at, approved_at) VALUES (:t, :n, :now, :now)"),
            {"t": TID, "n": name, "now": now},
        )
        c.execute(
            text(
                "INSERT INTO inbound_messages "
                "(id, tenant_id, channel_type, bot_key, external_update_id, status, "
                " contains_sensitive_data, created_at) "
                "VALUES (:id, :t, 'telegram', 'sreda', :uid, 'accepted', false, :now)"
            ),
            {"id": f"in_360_{update_id}", "t": TID, "uid": update_id, "now": now},
        )
        c.execute(
            text("INSERT INTO signup_attempts (channel, source_id_hash, attempted_at) "
                 "VALUES ('telegram', :h, :now)"),
            {"h": _signup_hash(CHAT), "now": now},
        )
        if deep:
            c.execute(text("INSERT INTO workspaces (id, tenant_id, name) VALUES (:i, :t, 'w')"),
                      {"i": f"ws_360_{CHAT}", "t": TID})
            c.execute(text("INSERT INTO users (id, tenant_id) VALUES (:i, :t)"),
                      {"i": f"user_360_{CHAT}", "t": TID})
            c.execute(
                text("INSERT INTO tenant_subscriptions (id, tenant_id, plan_id, feature_key, "
                     "status, created_at, updated_at) "
                     "VALUES (:i, :t, :p, 'smoke360', 'active', :now, :now)"),
                {"i": f"sub_360_{CHAT}", "t": TID, "p": PLAN_ID, "now": now},
            )


def _owner_count(owner_eng, table: str, col: str, val) -> int:
    with owner_eng.connect() as c:
        return c.execute(
            text(f"SELECT count(*) FROM {table} WHERE {col} = :v"),  # noqa: S608 — статические имена
            {"v": val},
        ).scalar_one()


def _assert_children_intact(owner_eng, tid: str = TID) -> None:
    assert _owner_count(owner_eng, "tenants", "id", tid) == 1, "tenants ТРОНУТ"
    assert _owner_count(owner_eng, "users", "tenant_id", tid) == 1, "users ТРОНУТ"
    assert _owner_count(owner_eng, "workspaces", "tenant_id", tid) == 1, "workspaces ТРОНУТ"
    assert _owner_count(owner_eng, "inbound_messages", "tenant_id", tid) >= 1, "inbound ТРОНУТ"


# ── класс бага: голая app-сессия слепа, tenant/maintenance — зрячие ────────


def test_bug_class_blind_app_vs_scoped_doors(owner, role_env):
    _seed(owner, name=f"{smoke._SENTINEL} {TID}", update_id="-360001")
    from sreda.db import session as dbs

    # голая app-сессия (старая дверь smoke) — 0 строк: так он и ослеп
    with dbs.get_session_factory()() as s:
        assert s.execute(text("SELECT 1 FROM tenants WHERE id = :t"), {"t": TID}).first() is None

    # tenant_session (дверь верификации) — строка видна глазами юзера
    with dbs.tenant_session(TID) as s:
        assert s.execute(text("SELECT 1 FROM tenants WHERE id = :t"), {"t": TID}).first() is not None

    # maintenance (дверь safety/чистки) — имя читается
    with dbs.privileged_session("monitor") as s:
        assert smoke._tenant_name(s, TID) == f"{smoke._SENTINEL} {TID}"


def test_maintenance_sighted_under_role_dsns(owner, role_env):
    assert smoke._maintenance_sighted() is True


@pytest.mark.parametrize("priv", ["SELECT", "UPDATE", "DELETE"])
def test_maintenance_sighted_requires_each_privilege(owner, role_env, priv):
    """sol R3: has_table_privilege('A,B') в PG = ЛЮБОЕ из прав (OR). Гейт обязан требовать
    ВСЕ три: без любого одного — не зрячий (иначе чистка встала бы с остатком)."""
    with owner.begin() as c:
        c.execute(text(f"REVOKE {priv} ON tenants FROM sreda_maintenance"))
    try:
        assert smoke._maintenance_sighted() is False, f"без {priv} гейт обязан отказать"
    finally:
        with owner.begin() as c:
            c.execute(text(f"GRANT {priv} ON tenants TO sreda_maintenance"))
    assert smoke._maintenance_sighted() is True, "права восстановлены — гейт снова зрячий"


# ── _has_foreign_activity: зряч под maintenance ────────────────────────────


def test_has_foreign_activity_sees_real_rows(owner, role_env):
    _seed(owner, name=f"{smoke._SENTINEL} {TID}", update_id="-360002")
    assert smoke._has_foreign_activity(TID) is False, "только наш отрицательный update_id"

    now = datetime.now(timezone.utc)
    with owner.begin() as c:  # чужая активность: реальный (положительный) update_id
        c.execute(
            text(
                "INSERT INTO inbound_messages "
                "(id, tenant_id, channel_type, bot_key, external_update_id, status, "
                " contains_sensitive_data, created_at) "
                "VALUES ('in_360_real', :t, 'telegram', 'sreda', '360003', 'accepted', false, :now)"
            ),
            {"t": TID, "now": now},
        )
    assert smoke._has_foreign_activity(TID) is True, (
        "реальная активность ОБЯЗАНА детектироваться (слепая сессия видела бы 0 → ложный «наш»)"
    )


# ── _mark_synthetic: маркер встаёт; пруф владения — ПОД замком той же транзакции ──


def test_mark_synthetic_sets_sentinel(owner, role_env):
    _seed(owner, name="smoke-360 пока без маркера", update_id="-360004", deep=False)
    marked, why = smoke._mark_synthetic(TID)
    assert marked is True, f"маркировка обязана пройти: {why}"
    with owner.connect() as c:
        name = c.execute(text("SELECT name FROM tenants WHERE id = :t"), {"t": TID}).scalar_one()
    assert name == f"{smoke._SENTINEL} {TID}", "точное маркер-имя не встало"


def test_mark_synthetic_refuses_foreign_activity(owner, role_env):
    # R2 sol/terra: пруф внутри маркировки под FOR UPDATE — имя реального тенанта не перезаписать
    _seed(owner, name="реальный тенант", update_id="360010", deep=False)  # реальный update_id
    marked, why = smoke._mark_synthetic(TID)
    assert marked is False and "актив" in why
    with owner.connect() as c:
        name = c.execute(text("SELECT name FROM tenants WHERE id = :t"), {"t": TID}).scalar_one()
    assert name == "реальный тенант", "имя реального тенанта ПЕРЕЗАПИСАНО"


# ── _cleanup_synthetic: помеченный тенант удаляется ЦЕЛИКОМ (глубокий граф) ──


def test_cleanup_deletes_marked_synthetic_deep_graph(owner, role_env):
    _seed(owner, name=f"{smoke._SENTINEL} {TID}", update_id="-360005")
    clean, why = smoke._cleanup_synthetic(TID, CHAT)
    assert clean is True, f"чистка обязана пройти полностью, why={why}"
    for tbl, col in (("tenants", "id"), ("inbound_messages", "tenant_id"),
                     ("users", "tenant_id"), ("workspaces", "tenant_id"),
                     ("tenant_subscriptions", "tenant_id")):
        assert _owner_count(owner, tbl, col, TID) == 0, f"{tbl}: синтетика осталась"
    assert _owner_count(owner, "signup_attempts", "source_id_hash", _signup_hash(CHAT)) == 0, (
        "signup_attempts: остаток (rate-limit 3/24ч ложно задушит следующие прогоны)"
    )


# ── safety-отказы не ослаблены фиксом; дети целы ───────────────────────────


def test_cleanup_refuses_unmarked_tenant(owner, role_env):
    _seed(owner, name="реальный-на-вид тенант без маркера", update_id="-360006")
    clean, why = smoke._cleanup_synthetic(TID, CHAT)
    assert clean is False and "маркер" in why
    _assert_children_intact(owner)


def test_cleanup_refuses_non_exact_marker_name(owner, role_env):
    # sol R1 CRITICAL: маркер В СЕРЕДИНЕ имени раньше проходил пре-чек (`in`), дети удалялись,
    # а root DELETE (LIKE 'маркер%') становился no-op. Теперь — точное имя, отказ, дети целы.
    _seed(owner, name=f"приманка {smoke._SENTINEL} {TID}", update_id="-360007")
    clean, why = smoke._cleanup_synthetic(TID, CHAT)
    assert clean is False and "маркер" in why
    _assert_children_intact(owner)


def test_cleanup_refuses_foreign_activity(owner, role_env):
    _seed(owner, name=f"{smoke._SENTINEL} {TID}", update_id="360008")  # реальный update_id
    clean, why = smoke._cleanup_synthetic(TID, CHAT)
    assert clean is False and "реальная активность" in why
    _assert_children_intact(owner)


# ── полу-флип: слепой maintenance → fail-closed отказ, не ложный PASS ──────


def test_blind_maintenance_fail_closed(owner, blind_env):
    _seed(owner, name=f"{smoke._SENTINEL} {TID}", update_id="-360009", deep=False)
    assert smoke._maintenance_sighted() is False, (
        "app-фолбэк maintenance обязан детектироваться как слепой"
    )
    clean, why = smoke._cleanup_synthetic(TID, CHAT)
    assert clean is False and "слеп" in why, f"слепая чистка обязана ОТКАЗАТЬ, а не «не существует»: {why}"
    assert _owner_count(owner, "tenants", "id", TID) == 1, "слепой прогон ТРОНУЛ данные"


# ── сквозной _run_smoke_locked: стабы inbound-пути, реальные роль-DSN ──────


class _StubTI:
    """Мини-двойник telegram_inbound: провижн (тенант + подписка + inbound-строка с нашим
    отрицательным update_id — как настоящий identity/ReAct-путь, который в этом тесте не
    поднять) и трейс хода сеются owner'ом; ответ шлётся через подменённый _run_smoke_locked'ом
    telegram_client_for → верификация/пруф/маркировка/чистка скрипта идут по НАСТОЯЩИМ
    роль-дверям и по РЕАЛЬНЫМ строкам (включая inbound для ownership-пруфа)."""

    def __init__(self, owner_eng, sub_status: str = "active") -> None:
        self._owner = owner_eng
        self._sub_status = sub_status
        self.telegram_client_for = None  # перезапишет _run_smoke_locked

    async def handle_telegram_update(self, update, bot_key=None):  # noqa: ANN001
        txt = update["message"]["text"]
        chat_id = update["message"]["chat"]["id"]
        uid = update["update_id"]
        tid = f"tenant_tg_{chat_id}"
        now = datetime.now(timezone.utc)
        if txt == "/start":
            with self._owner.begin() as c:
                c.execute(
                    text("INSERT INTO tenants (id, name, created_at, approved_at) "
                         "VALUES (:t, 'e2e-360', :now, :now)"), {"t": tid, "now": now})
                c.execute(
                    text("INSERT INTO tenant_subscriptions (id, tenant_id, plan_id, feature_key, "
                         "status, created_at, updated_at) "
                         "VALUES (:i, :t, :p, 'smoke360', :st, :now, :now)"),
                    {"i": f"sub_{tid}", "t": tid, "p": PLAN_ID, "st": self._sub_status, "now": now})
                c.execute(
                    text("INSERT INTO inbound_messages "
                         "(id, tenant_id, channel_type, bot_key, external_update_id, status, "
                         " contains_sensitive_data, created_at) "
                         "VALUES (:i, :t, 'telegram', 'sreda', :u, 'accepted', false, :now)"),
                    {"i": f"in_e2e_{uid}", "t": tid, "u": str(uid), "now": now})
            return
        from sreda.db.models.react_trace import ReactTurnTrace
        with Session(self._owner) as s:
            s.add(ReactTurnTrace(
                id=f"trace_{tid}", tenant_id=tid, turn_key=f"turn_{tid}",
                status="done", outcome="ok", reply_text="здравствуйте",
                created_at=now,
            ))
            s.commit()
        await self.telegram_client_for().send_message(chat_id=chat_id, text="здравствуйте")


class _StubAA:
    send_admin_alert = None
    _post_telegram_sync = None
    _post_max_sync = None


def test_run_smoke_locked_end_to_end_pass_and_clean(owner, role_env, monkeypatch):
    """Главный wiring-тест (terra/sol R1 MAJOR): верификация created/sub/trace ВНУТРИ
    _run_smoke_locked обязана идти через tenant_session. Мутация «вернуть голую
    get_session_factory» → created=None → rc=1 → тест красный."""
    _wipe(owner, TID_E2E, CHAT_E2E)
    monkeypatch.setattr("sreda.workers.message_queue.is_queue_enabled_for", lambda _t: False)

    rc = asyncio.run(smoke._run_smoke_locked(
        "sreda", CHAT_E2E, TID_E2E, _StubTI(owner), _StubAA(), text, "<<MAXSTEPS-MARKER>>"))
    assert rc == 0, "здоровый онбординг + полная чистка обязаны дать PASS (exit 0)"
    for tbl, col in (("tenants", "id"), ("tenant_subscriptions", "tenant_id"),
                     ("react_turn_trace", "tenant_id"), ("inbound_messages", "tenant_id")):
        assert _owner_count(owner, tbl, col, TID_E2E) == 0, f"{tbl}: e2e оставил мусор"
    assert _owner_count(owner, "signup_attempts", "source_id_hash", _signup_hash(CHAT_E2E)) == 0


def test_run_smoke_locked_blind_maintenance_aborts(owner, blind_env, monkeypatch):
    """Полу-флип на уровне ПОЛНОГО прогона: sight-гейт обязан дать ABORT (exit 2)
    ДО каких-либо действий (субагент R2: e2e покрывал только happy-path)."""
    _wipe(owner, TID_E2E, CHAT_E2E)
    monkeypatch.setattr("sreda.workers.message_queue.is_queue_enabled_for", lambda _t: False)
    rc = asyncio.run(smoke._run_smoke_locked(
        "sreda", CHAT_E2E, TID_E2E, _StubTI(owner), _StubAA(), text, "<<MAXSTEPS-MARKER>>"))
    assert rc == 2, "слепой maintenance обязан дать ABORT (2), не PASS/FAIL"
    assert _owner_count(owner, "tenants", "id", TID_E2E) == 0, "слепой прогон что-то создал/оставил"


def test_run_smoke_locked_broken_onboarding_exit_1(owner, role_env, monkeypatch):
    """Сломанный онбординг (подписка не active) → exit 1, и чистка в finally всё равно
    убирает синтетику (субагент R2: инвариант exit-кодов на уровне полного прогона)."""
    _wipe(owner, TID_E2E, CHAT_E2E)
    monkeypatch.setattr("sreda.workers.message_queue.is_queue_enabled_for", lambda _t: False)
    rc = asyncio.run(smoke._run_smoke_locked(
        "sreda", CHAT_E2E, TID_E2E, _StubTI(owner, sub_status="pending_payment"), _StubAA(),
        text, "<<MAXSTEPS-MARKER>>"))
    assert rc == 1, "неверный тир подписки = онбординг сломан = exit 1"
    assert _owner_count(owner, "tenants", "id", TID_E2E) == 0, "finally-чистка не убрала синтетику"
