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


def test_create_category_rejects_control_chars(ctx):
    """R1 (Codex high): контрол-символы ломают построчный контракт created:<id>:<name> → отсекаем."""
    s, tools = ctx
    out = tools["create_memory_category"].invoke({"name": "ма\nшина"})
    assert out.startswith("error:") and ("спецсимвол" in out or "переводы" in out)
    assert not [c for c in _repo(s).list_categories("t1", "u1") if "\n" in c.name]  # в БД не записалась


def test_save_fact_rejects_control_char_category(ctx):
    """R1: category с контрол-символом → error, факт НЕ уходит молча в «Общее»."""
    s, tools = ctx
    out = tools["save_core_fact"].invoke({"content": "тест", "category": "ра\tбота"})
    assert out.startswith("error:") and "имя" in out


def test_save_fact_category_race_reresolves(ctx, monkeypatch):
    """R1 (Codex high+medium): гонка — категория создана конкурентно между list и create.
    Симуляция: 1-й list_categories → [] (не нашли), create ловит CategoryNameConflict (категория УЖЕ
    есть), ре-резолв (2-й list) находит её → факт сохранён, инструмент НЕ падает."""
    s, tools = ctx
    tools["create_memory_category"].invoke({"name": "машина"})  # категория уже существует + закоммичена
    real_list = MemoryRepository.list_categories
    state = {"n": 0}

    def fake_list(self, t, u):
        state["n"] += 1
        return [] if state["n"] == 1 else real_list(self, t, u)

    monkeypatch.setattr(MemoryRepository, "list_categories", fake_list)
    out = tools["save_core_fact"].invoke({"content": "купить ТО", "category": "машина"})
    monkeypatch.undo()
    assert out.startswith("saved_core:")  # НЕ error — ре-резолв нашёл существующую
    cat = next(c for c in _repo(s).list_categories("t1", "u1") if c.name == "машина")
    assert any("ТО" in f.content for f in _repo(s).list_facts_in_category("t1", "u1", cat.id))


def test_319_sticky_prefixes_match_real_tools(ctx):
    """#319 R2 (субагент MINOR): связка контракта — РЕАЛЬНЫЕ ответы memory-write инструментов обязаны
    начинаться с `_MEMORY_WRITE_OK_PREFIXES` (продление двери серии). Иначе координированная смена
    формата в tools.py (+его тестов) молча оставила бы константу протухшей → дверь никогда не
    открывается → серия тихо регрессирует в per-item confirm."""
    from sreda.runtime.react_loop import _MEMORY_WRITE_OK_PREFIXES
    s, tools = ctx
    outs = [
        tools["create_memory_category"].invoke({"name": "куры"}),
        tools["save_core_fact"].invoke({"content": "вес крыльев 615", "category": "куры"}),
        tools["save_episode"].invoke({"summary": "взвесил бёдра 865"}),
    ]
    for out in outs:
        assert out.startswith(_MEMORY_WRITE_OK_PREFIXES), out


def test_325_exact_replay_same_operation(ctx):
    """#325: повтор ТОГО ЖЕ operation_id (re-execute узла на resume) → сохранённый payload, БЕЗ второй
    строки; НОВЫЙ op_id (новый ход) → новая строка."""
    from sreda.db.models import AssistantMemory
    from sreda.runtime.planner.tool_runtime import ToolRuntimeContext, bind_tool_runtime
    s, tools = ctx

    def _ctx(op):
        return ToolRuntimeContext(operation_id=op, execution_id="e", step_id="s1",
                                  tool_name="save_episode", tenant_id="t1", user_id="u1",
                                  turn_key="tk", channel="react", thread_id="th", origin="react")

    def _n_episodes():
        return s.query(AssistantMemory).filter(AssistantMemory.tenant_id == "t1",
                                               AssistantMemory.tier == "episodic").count()

    with bind_tool_runtime(_ctx("op-325-a")):
        out1 = tools["save_episode"].invoke({"summary": "крылья 615"})
        out2 = tools["save_episode"].invoke({"summary": "крылья 615"})  # re-execute того же вызова
    assert out1.startswith("saved_episode:") and out1 == out2, (out1, out2)
    assert _n_episodes() == 1, "повтор того же op_id НЕ создаёт вторую строку"
    with bind_tool_runtime(_ctx("op-325-b")):  # другой ход → другой op → честная новая запись
        out3 = tools["save_episode"].invoke({"summary": "крылья 615"})
    assert out3.startswith("saved_episode:") and out3 != out1
    assert _n_episodes() == 2


def test_325_legacy_path_without_ctx_unchanged(ctx):
    """#325: вне ReAct-ctx (легаси) — self-commit как раньше, каждый вызов пишет (идемпотентности нет)."""
    from sreda.db.models import AssistantMemory
    s, tools = ctx
    tools["save_episode"].invoke({"summary": "легаси раз"})
    tools["save_episode"].invoke({"summary": "легаси раз"})
    n = s.query(AssistantMemory).filter(AssistantMemory.tenant_id == "t1",
                                        AssistantMemory.tier == "episodic").count()
    assert n >= 2  # легаси-семантика не менялась (без ctx нет ключа идемпотентности)


def test_325_replay_create_category_and_fact_with_category(ctx):
    """#325 R2 (субагент MINOR-3): replay остальных write-инструментов. Тот же op_id → 1 строка/1
    категория, идентичный payload; резолв категории ВНУТРИ мутации (replay его не запускает —
    иначе побочный эффект утекал бы на replay/error-путях, R1 оба Codex)."""
    from sreda.db.models import AssistantMemory
    from sreda.runtime.planner.tool_runtime import ToolRuntimeContext, bind_tool_runtime
    s, tools = ctx

    def _ctx(op, tool):
        return ToolRuntimeContext(operation_id=op, execution_id="e", step_id="s1",
                                  tool_name=tool, tenant_id="t1", user_id="u1",
                                  turn_key="tk", channel="react", thread_id="th", origin="react")

    # create_memory_category: replay того же op → одна категория, тот же payload
    with bind_tool_runtime(_ctx("op-325-cat", "create_memory_category")):
        c1 = tools["create_memory_category"].invoke({"name": "рыбалка325"})
        c2 = tools["create_memory_category"].invoke({"name": "рыбалка325"})
    assert c1.startswith("created:") and c1 == c2, (c1, c2)
    cats = [c for c in _repo(s).list_categories("t1", "u1") if "рыбалка325" in c.name]
    assert len(cats) == 1, "replay НЕ создаёт вторую категорию"

    # save_core_fact С НОВОЙ категорией: replay → один факт, ОДНА категория (резолв не пере-запущен)
    with bind_tool_runtime(_ctx("op-325-fact", "save_core_fact")):
        f1 = tools["save_core_fact"].invoke({"content": "люблю окуней", "category": "охота325"})
        f2 = tools["save_core_fact"].invoke({"content": "люблю окуней", "category": "охота325"})
    assert f1.startswith("saved_core:") and f1 == f2, (f1, f2)
    facts = s.query(AssistantMemory).filter(AssistantMemory.tenant_id == "t1",
                                            AssistantMemory.tier == "core").count()
    assert facts == 1, "replay НЕ создаёт второй факт"
    cats2 = [c for c in _repo(s).list_categories("t1", "u1") if "охота325" in c.name]
    assert len(cats2) == 1, "replay НЕ создаёт вторую категорию через резолв"


def test_325_replay_does_not_resurrect_deleted_category(ctx):
    """#325 R3 (субагент R2 MAJOR — mutation-hole): лок на «резолв ВНУТРИ мутации». Replay уже
    закоммиченной операции НЕ должен пере-запускать резолв категории: удаляем категорию → replay того же
    op_id → payload сохранённый, категория ОСТАЁТСЯ удалённой. R1-форма (резолв снаружи _idem_write)
    пере-резолвила бы и ВОСКРЕСИЛА категорию — этот тест на ней красный (mutation-verified)."""
    from sreda.db.models import AssistantMemory
    from sreda.runtime.planner.tool_runtime import ToolRuntimeContext, bind_tool_runtime
    s, tools = ctx
    c = ToolRuntimeContext(operation_id="op-325-res", execution_id="e", step_id="s1",
                           tool_name="save_core_fact", tenant_id="t1", user_id="u1",
                           turn_key="tk", channel="react", thread_id="th", origin="react")
    with bind_tool_runtime(c):
        f1 = tools["save_core_fact"].invoke({"content": "клюёт на зорьке", "category": "клёв325"})
    assert f1.startswith("saved_core:")
    # удаляем категорию (как будто юзер снёс её в мини-аппе); сначала факт — FK на категорию
    cat = next(x for x in _repo(s).list_categories("t1", "u1") if "клёв325" in x.name)
    for m in s.query(AssistantMemory).filter(AssistantMemory.category_id == cat.id).all():
        s.delete(m)
    s.delete(cat)
    s.commit()
    with bind_tool_runtime(c):  # replay ТОГО ЖЕ op (re-execute узла)
        f2 = tools["save_core_fact"].invoke({"content": "клюёт на зорьке", "category": "клёв325"})
    assert f2 == f1, "replay возвращает сохранённый payload"
    assert not [x for x in _repo(s).list_categories("t1", "u1") if "клёв325" in x.name], \
        "replay НЕ воскрешает удалённую категорию (резолв не пере-запущен)"
