"""#262b: голосовые инструменты памяти — create_memory_category + save_core_fact(category=...).

Движок с PRAGMA foreign_keys=ON (g-061: FK/каскад реально энфорсятся). Проверяет ИСПОЛНЕНИЕ замыканий:
создание категории, резерв «Общее», дубль, факт в названную категорию (создаётся), факт без категории → Common.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from sreda.db.base import Base
from sreda.db.repositories.memory import MemoryRepository
from sreda.db.repositories.seed import SeedRepository
from sreda.runtime.tools import build_memory_tools
from sreda.services.embeddings import FakeEmbeddingClient


@pytest.fixture()
def ctx():
    eng = create_engine("sqlite:///:memory:")

    @event.listens_for(eng, "connect")
    def _fk(dbapi, _rec):  # noqa: ANN001
        dbapi.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    s = Session(eng)
    SeedRepository(s).ensure_tenant_bundle(
        tenant_id="t1", tenant_name="t", workspace_id="w", workspace_name="w",
        user_id="u1", telegram_account_id="1", assistant_id="a", assistant_name="a")
    s.commit()
    tools = {
        t.name: t
        for t in build_memory_tools(
            session=s, tenant_id="t1", user_id="u1", embedding_client=FakeEmbeddingClient())
    }
    yield s, tools
    s.close()
    eng.dispose()


def _repo(s):
    return MemoryRepository(s)


def test_create_category_creates_in_db(ctx):
    s, tools = ctx
    out = tools["create_memory_category"].invoke({"name": "машина"})
    assert out.startswith("created:")
    assert "машина" in [c.name for c in _repo(s).list_categories("t1", "u1")]


def test_create_reserved_common_is_error(ctx):
    s, tools = ctx
    out = tools["create_memory_category"].invoke({"name": "Общее"})
    assert out.startswith("error:") and "зарезервировано" in out


def test_create_duplicate_is_error(ctx):
    s, tools = ctx
    tools["create_memory_category"].invoke({"name": "Работа"})
    out = tools["create_memory_category"].invoke({"name": "работа"})  # норм-дубль (лемма)
    assert out.startswith("error:") and "уже есть" in out


def test_save_fact_to_named_category_creates_it(ctx):
    s, tools = ctx
    out = tools["save_core_fact"].invoke({"content": "купить ТО", "category": "машина"})
    assert out.startswith("saved_core:")
    cat = next(c for c in _repo(s).list_categories("t1", "u1") if c.name == "машина")
    facts = _repo(s).list_facts_in_category("t1", "u1", cat.id)
    assert any("ТО" in f.content for f in facts)


def test_save_fact_to_existing_category_reuses(ctx):
    s, tools = ctx
    tools["create_memory_category"].invoke({"name": "Здоровье"})
    tools["save_core_fact"].invoke({"content": "аллергия на орехи", "category": "здоровье"})  # тот же по норме
    cats = [c for c in _repo(s).list_categories("t1", "u1") if c.name_normalized == "здоровье"]
    assert len(cats) == 1  # НЕ создал второй
    assert _repo(s).count_facts_in_category("t1", "u1", cats[0].id) == 1


def test_save_fact_without_category_goes_to_common(ctx):
    s, tools = ctx
    out = tools["save_core_fact"].invoke({"content": "живу в Москве"})
    assert out.startswith("saved_core:")
    common = next(c for c in _repo(s).list_categories("t1", "u1") if c.is_system)
    facts = _repo(s).list_facts_in_category("t1", "u1", common.id)
    assert any("Москве" in f.content for f in facts)
