"""#138 Ф3-b — tenant_id в react_checkpoint / react_checkpoint_write.

Штамп тенанта из tenant_ctx в saver'е (put / put_writes), NULL без ctx (fail-closed
видимость под RLS Ф3-c, не падаем), cross-check анти-подмены (чужой тред → RuntimeError),
не-затирание не-NULL tenant_id NULL'ом, и сверка деривации durable-ключа бэкфилла 0081
с боевой react_loop._durable_thread_id.
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.session import tenant_ctx
from sreda.runtime.react_checkpoint_saver import EncryptedSqlCheckpointSaver


@pytest.fixture()
def db():
    """Файловый sqlite-движок с нашими таблицами + factory (как в test_react_checkpoint_saver_193)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield SF, engine
    engine.dispose()
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture()
def saver(db):
    SF, _ = db
    # owner_session_factory=SF: в юнит-тестах (sqlite без RLS) привилегированный owner-lookup
    # cross-check'а ходит в ту же тестовую БД (в проде None → реальный privileged_session("gc")).
    return EncryptedSqlCheckpointSaver(session_factory=SF, owner_session_factory=SF)


@pytest.fixture()
def _no_tenant_ctx():
    """Гарантированно чистый tenant_ctx (тесты выше по файлу не должны протекать)."""
    tok = tenant_ctx.set(None)
    yield
    tenant_ctx.reset(tok)


def _cfg(thread="react-v1:hmacT", ns="", cp_id=None):
    conf = {"thread_id": thread, "checkpoint_ns": ns}
    if cp_id is not None:
        conf["checkpoint_id"] = cp_id
    return {"configurable": conf}


def _ckpt(cp_id, text_content="напомни полить цветы"):
    ck = empty_checkpoint()
    ck["id"] = cp_id
    ck["channel_values"] = {"messages": [HumanMessage(content=text_content)]}
    return ck


def _put_as(saver, tenant, cfg, ck, meta=None):
    tok = tenant_ctx.set(tenant)
    try:
        return saver.put(cfg, ck, meta or {}, {})
    finally:
        tenant_ctx.reset(tok)


def _put_writes_as(saver, tenant, cfg, writes, task_id="t1"):
    tok = tenant_ctx.set(tenant)
    try:
        saver.put_writes(cfg, writes, task_id=task_id)
    finally:
        tenant_ctx.reset(tok)


# --- штамп из ctx -----------------------------------------------------------

def test_put_stamps_tenant_from_ctx(saver):
    ck = _ckpt("00000000-0000-0000-0000-000000000001")
    _put_as(saver, "tenant_A", _cfg(), ck)
    with saver._sf() as s:
        row = s.execute(text(
            "select tenant_id from react_checkpoint")).scalar_one()
    assert row == "tenant_A"


def test_put_writes_stamps_tenant_from_ctx(saver):
    ck = _ckpt("00000000-0000-0000-0000-000000000002")
    _put_as(saver, "tenant_A", _cfg(), ck)
    _put_writes_as(saver, "tenant_A", _cfg(cp_id=ck["id"]),
                   [("messages", "v0"), ("__interrupt__", "пауза")])
    with saver._sf() as s:
        tenants = [r[0] for r in s.execute(text(
            "select tenant_id from react_checkpoint_write")).all()]
    assert tenants and all(t == "tenant_A" for t in tenants)


# --- без ctx → NULL, не падает ---------------------------------------------

def test_put_without_ctx_null_tenant_and_no_crash(saver, _no_tenant_ctx, caplog):
    ck = _ckpt("00000000-0000-0000-0000-000000000003")
    with caplog.at_level("WARNING"):
        saver.put(_cfg(), ck, {}, {})
        saver.put_writes(_cfg(cp_id=ck["id"]), [("messages", "v")], task_id="t1")
    with saver._sf() as s:
        cp_t = s.execute(text("select tenant_id from react_checkpoint")).scalar_one()
        w_t = s.execute(text("select tenant_id from react_checkpoint_write")).scalar_one()
    assert cp_t is None and w_t is None
    # деградация видна в логах
    assert any("без tenant_ctx" in r.message for r in caplog.records)


def test_reput_without_ctx_does_not_clobber_tenant(saver, _no_tenant_ctx):
    """UPDATE существующей строки НЕ затирает не-NULL tenant_id NULL'ом."""
    ck = _ckpt("00000000-0000-0000-0000-000000000004")
    _put_as(saver, "tenant_A", _cfg(), ck)
    saver.put(_cfg(), ck, {"step": 2}, {})  # re-put того же checkpoint БЕЗ ctx
    with saver._sf() as s:
        row = s.execute(text("select tenant_id from react_checkpoint")).scalar_one()
    assert row == "tenant_A"


# --- cross-check анти-подмены ----------------------------------------------

def test_put_cross_tenant_raises(saver):
    ck = _ckpt("00000000-0000-0000-0000-000000000005")
    _put_as(saver, "tenant_A", _cfg(), ck)
    ck2 = _ckpt("00000000-0000-0000-0000-000000000006", "чужое")
    with pytest.raises(RuntimeError, match="tenant mismatch"):
        _put_as(saver, "tenant_B", _cfg(), ck2)
    # чужая строка НЕ дописана
    with saver._sf() as s:
        n = s.execute(text("select count(*) from react_checkpoint")).scalar()
    assert n == 1


def test_put_writes_cross_tenant_raises(saver):
    ck = _ckpt("00000000-0000-0000-0000-000000000007")
    _put_as(saver, "tenant_A", _cfg(), ck)
    with pytest.raises(RuntimeError, match="tenant mismatch"):
        _put_writes_as(saver, "tenant_B", _cfg(cp_id=ck["id"]), [("messages", "v")])
    with saver._sf() as s:
        n = s.execute(text("select count(*) from react_checkpoint_write")).scalar()
    assert n == 0


def test_put_same_tenant_ok_and_backfills_null(saver, _no_tenant_ctx):
    """Тот же тенант — не конфликт; повторный put ПОД ctx проставляет tenant в NULL-строку."""
    ck = _ckpt("00000000-0000-0000-0000-000000000008")
    saver.put(_cfg(), ck, {}, {})  # легаси-строка без ctx → NULL
    _put_as(saver, "tenant_A", _cfg(), ck)  # re-put под ctx
    _put_as(saver, "tenant_A", _cfg(cp_id=ck["id"]),
            _ckpt("00000000-0000-0000-0000-000000000009"))  # и новый checkpoint — ок
    with saver._sf() as s:
        tenants = [r[0] for r in s.execute(text(
            "select tenant_id from react_checkpoint order by checkpoint_id")).all()]
    assert tenants == ["tenant_A", "tenant_A"]


# --- cross-check НЕ слепнет под RLS (владение читается ВНЕ app-скоупа) -------

def test_cross_check_survives_rls_blind_app_session(db):
    """Ф5-регрессия (R1 MAJOR): app-сессия записи под RLS НЕ видит чужой чекпоинт, но
    cross-check ВСЁ РАВНО ловит mismatch, т.к. владение читается привилегированной сессией.

    Две РАЗНЫЕ БД моделируют RLS-слепоту: app-БД пуста (чужая строка «отфильтрована» ролью
    sreda_app), owner-БД содержит чужой tenant_B (её видит maintenance-роль). Если бы lookup
    шёл через записывающую app-сессию (как в баге) — existing вернулся бы None и подмена
    прошла бы молча. Тест краснеет при откате фикса."""
    app_SF, _ = db
    fd, opath = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    oeng = create_engine(f"sqlite:///{opath}")
    Base.metadata.create_all(oeng)
    owner_SF = sessionmaker(bind=oeng, autoflush=False, autocommit=False)
    try:
        # заселяем owner-БД чужим тредом tenant_B (тот же thread_id по умолчанию)
        seeder = EncryptedSqlCheckpointSaver(session_factory=owner_SF, owner_session_factory=owner_SF)
        _put_as(seeder, "tenant_B", _cfg(), _ckpt("00000000-0000-0000-0000-0000000000b1", "чужое"))
        # saver ПИШЕТ в пустую app-БД, а владение проверяет через owner-БД (привилегированный путь)
        saver = EncryptedSqlCheckpointSaver(session_factory=app_SF, owner_session_factory=owner_SF)
        with pytest.raises(RuntimeError, match="tenant mismatch"):
            _put_as(saver, "tenant_A", _cfg(), _ckpt("00000000-0000-0000-0000-0000000000a1"))
        # cross-check сработал ДО upsert'а — в app-БД ничего не записано
        with app_SF() as s:
            assert s.execute(text("select count(*) from react_checkpoint")).scalar() == 0
    finally:
        oeng.dispose()
        try:
            os.unlink(opath)
        except OSError:
            pass


def test_owner_lookup_goes_through_privileged_session(db, monkeypatch):
    """Прод-путь (owner_session_factory не задан) читает владение через privileged_session('gc'),
    а НЕ через записывающую app-сессию (иначе под RLS Ф5 cross-check ослеп бы). Подменяем
    privileged_session на тестовую БД и фиксируем, что он вызван с reason='gc'."""
    import sreda.runtime.react_checkpoint_saver as mod

    SF, _ = db
    calls = []

    @contextmanager
    def fake_priv(reason):
        calls.append(reason)
        s = SF()
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr(mod, "privileged_session", fake_priv)
    saver = EncryptedSqlCheckpointSaver(session_factory=SF)  # owner_sf=None → прод-путь
    _put_as(saver, "tenant_A", _cfg(), _ckpt("00000000-0000-0000-0000-0000000000c1"))
    assert calls == ["gc"]  # владение проверено ИМЕННО привилегированной сессией


# --- деривация durable-ключа (бэкфилл 0081 vs react_loop) -------------------

def _load_migration_0081():
    root = Path(__file__).resolve().parents[2]
    path = root / "migrations" / "versions" / "20260709_0081_checkpoint_tenant_backfill.py"
    spec = importlib.util.spec_from_file_location("migration_0081", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_durable_matches_react_loop():
    """Формула _durable в миграции 0081 = боевой react_loop._durable_thread_id (байт-в-байт)."""
    from sreda.config.settings import get_settings
    from sreda.runtime import react_loop as RL

    mig = _load_migration_0081()
    secret = get_settings().encryption_key
    assert secret  # conftest даёт детерминированный тестовый ключ
    for base in ("react:tenant_max_40921122:40921122", "react:tenant_tg_1:1", "x"):
        assert mig._durable(secret, base) == RL._durable_thread_id(base)


def test_backfill_collision_abort():
    """Один base у двух разных tenant_id → RuntimeError с перечнем (не молча)."""
    mig = _load_migration_0081()
    ok = mig._collect_tenant_by_base([
        ("tenant_A", "base1"), ("tenant_A", "base1"), ("tenant_B", "base2"),
        (None, "base3"), ("tenant_C", None),
    ])
    assert ok == {"base1": "tenant_A", "base2": "tenant_B"}
    with pytest.raises(RuntimeError, match="collision"):
        mig._collect_tenant_by_base([("tenant_A", "base1"), ("tenant_B", "base1")])
