# -*- coding: utf-8 -*-
"""#143 Phase B — чек-листы «по описанию» работают единообразно с задачами.

Контракт фазы B:
  * list_checklist_items — ЧИТАЮЩИЙ шаг: фильтр-оставить-ВСЕХ (НЕ top-1) по
    фрагменту названия пункта сразу во всех активных списках; пусто → empty;
    различимые id-free метки (название пункта + название списка).
  * mark_checklist_item_done / delete_checklist_item — мутируют строго по
    item_id, с ownership-check ДО изменения (чужой tenant/user → item_not_found,
    мутации НЕТ — изоляция семей, launch-инвариант).
  * презентер нового Ok-варианта не светит внутренние id.

Эти тесты RED-before-impl для машинно-проверяемого ядра фазы B.
"""
from __future__ import annotations

import os
import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# EncryptedString требует ключ (как в test_checklists.py).
os.environ.setdefault(
    "SREDA_ENCRYPTION_KEY",
    "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
)

from sreda.db.base import Base  # noqa: E402
from sreda.db.models.checklists import ChecklistItem  # noqa: E402
from sreda.db.models.core import Tenant, User  # noqa: E402
from sreda.services.checklists import ChecklistService  # noqa: E402
from sreda.services.composer.breakdown_messages import BREAKDOWN_POOL  # noqa: E402
from sreda.services.composer.presenters import render_display_text  # noqa: E402
from sreda.services.housewife_chat_tools import build_housewife_tools  # noqa: E402
from sreda.services.tool_schemas.housewife import (  # noqa: E402
    ListChecklistItemsEmpty,
    ListChecklistItemsOk,
    ListChecklistItemsRow,
    parse_tool_output,
)


_ID_RE = re.compile(r"\b\w+_[0-9a-f]{24}\b")


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    # «Своя» семья u1@t1 + «чужая» семья u2@t2 (для ownership-теста).
    s.add(Tenant(id="t1", name="T1"))
    s.add(User(id="u1", tenant_id="t1", telegram_account_id="1"))
    s.add(Tenant(id="t2", name="T2"))
    s.add(User(id="u2", tenant_id="t2", telegram_account_id="2"))
    s.commit()
    yield s
    s.close()


@pytest.fixture
def tools(session):
    return {
        t.name: t
        for t in build_housewife_tools(
            session=session, tenant_id="t1", user_id="u1",
        )
    }


# ---------------------------------------------------------------------------
# list_checklist_items — фильтр-оставить-ВСЕХ (не top-1) + empty + метки
# ---------------------------------------------------------------------------


def test_list_checklist_items_two_matches_across_lists(tools):
    """2 совпадения в РАЗНЫХ списках → Ok с 2 строками (не top-1)."""
    tools["add_checklist_items"].invoke(
        {"list_id_or_title": "Дача", "items": ["Лопата", "Грабли"]}
    )
    tools["add_checklist_items"].invoke(
        {"list_id_or_title": "Гараж", "items": ["Лопата снегоуборочная", "Масло"]}
    )
    raw = tools["list_checklist_items"].invoke({"title_match": "лопата"})
    parsed = parse_tool_output("list_checklist_items", raw)
    assert isinstance(parsed, ListChecklistItemsOk), raw
    assert len(parsed.items) == 2, [r.item_title for r in parsed.items]
    lists = {r.list_title for r in parsed.items}
    assert lists == {"Дача", "Гараж"}


def test_list_checklist_items_zero_matches_is_empty(tools):
    tools["add_checklist_items"].invoke(
        {"list_id_or_title": "Дача", "items": ["Лопата"]}
    )
    raw = tools["list_checklist_items"].invoke({"title_match": "несуществующее"})
    parsed = parse_tool_output("list_checklist_items", raw)
    assert isinstance(parsed, ListChecklistItemsEmpty), raw


def test_list_checklist_items_list_title_match_narrows(tools):
    """list_title_match сужает поиск до конкретного списка."""
    tools["add_checklist_items"].invoke(
        {"list_id_or_title": "Дача", "items": ["Лопата"]}
    )
    tools["add_checklist_items"].invoke(
        {"list_id_or_title": "Гараж", "items": ["Лопата снегоуборочная"]}
    )
    raw = tools["list_checklist_items"].invoke(
        {"title_match": "лопата", "list_title_match": "дача"}
    )
    parsed = parse_tool_output("list_checklist_items", raw)
    assert isinstance(parsed, ListChecklistItemsOk)
    assert len(parsed.items) == 1
    assert parsed.items[0].list_title == "Дача"


def test_list_checklist_items_display_labels_distinct_with_list_names():
    """Различимые id-free метки содержат названия ОБОИХ списков."""
    _HEX = "0" * 24
    model = ListChecklistItemsOk(items=[
        ListChecklistItemsRow(
            item_id=f"clitem_{_HEX}", item_title="Лопата",
            list_title="Дача", list_id=f"checklist_{_HEX}",
            item_status="pending"),
        ListChecklistItemsRow(
            item_id=f"clitem_{_HEX}", item_title="Лопата снегоуборочная",
            list_title="Гараж", list_id=f"checklist_{_HEX}",
            item_status="pending"),
    ])
    summary = model.display_summary
    assert "Дача" in summary
    assert "Гараж" in summary
    # обе разные метки присутствуют → выбор различим
    assert "Лопата снегоуборочная" in summary


def test_list_checklist_items_display_disambiguates_identical_labels():
    """Полностью совпадающие метки получают порядковый суффикс."""
    _HEX = "0" * 24
    cid = f"checklist_{_HEX}"
    model = ListChecklistItemsOk(items=[
        ListChecklistItemsRow(item_id=f"clitem_{_HEX}", item_title="молоко",
                              list_title="Покупки", list_id=cid, item_status="pending"),
        ListChecklistItemsRow(item_id=f"clitem_{_HEX}", item_title="молоко",
                              list_title="Покупки", list_id=cid, item_status="done"),
    ])
    summary = model.display_summary
    # вторая идентичная метка различима суффиксом
    assert "#2" in summary


# ---------------------------------------------------------------------------
# mark / delete по item_id — успех + ownership (чужой tenant/user)
# ---------------------------------------------------------------------------


def test_mark_checklist_item_done_by_id_success(tools, session):
    tools["add_checklist_items"].invoke(
        {"list_id_or_title": "Дача", "items": ["Лопата"]}
    )
    svc = ChecklistService(session)
    item = svc.list_items(
        list_id=svc.list_active(tenant_id="t1", user_id="u1")[0].id
    )[0]
    r = tools["mark_checklist_item_done"].invoke({"item_id": item.id})
    assert r.startswith(f"ok:done:{item.id}:"), r
    session.expire_all()
    refreshed = session.get(ChecklistItem, item.id)
    assert refreshed.status == "done"


def test_delete_checklist_item_by_id_success(tools, session):
    tools["add_checklist_items"].invoke(
        {"list_id_or_title": "Дача", "items": ["Лопата", "Грабли"]}
    )
    svc = ChecklistService(session)
    list_id = svc.list_active(tenant_id="t1", user_id="u1")[0].id
    item = [i for i in svc.list_items(list_id=list_id) if i.title == "Лопата"][0]
    # захватываем id ДО удаления: атомарный DELETE (synchronize_session=False)
    # делает ORM-объект нечитаемым после commit (строка удалена в БД).
    target_id = item.id
    r = tools["delete_checklist_item"].invoke({"item_id": target_id})
    assert r.startswith(f"ok:deleted:{target_id}:"), r
    session.expire_all()
    assert session.get(ChecklistItem, target_id) is None


def test_mark_foreign_item_is_item_not_found_no_mutation(tools, session):
    """Чужой пункт (другая семья t2/u2) → item_not_found, мутации НЕТ."""
    foreign = ChecklistService(session)
    cl = foreign.create_list(tenant_id="t2", user_id="u2", title="Чужой список")
    [item], _ = foreign.add_items(list_id=cl.id, items=["Секрет"])
    foreign_id = item.id

    # tools построены для t1/u1 — НЕ должны трогать чужой пункт.
    r = tools["mark_checklist_item_done"].invoke({"item_id": foreign_id})
    assert r == "error: item_not_found", r
    # id не светим в ошибке
    assert foreign_id not in r
    session.expire_all()
    refreshed = session.get(ChecklistItem, foreign_id)
    assert refreshed.status == "pending", "чужой пункт изменён — нарушена изоляция"


def test_delete_foreign_item_is_item_not_found_no_mutation(tools, session):
    foreign = ChecklistService(session)
    cl = foreign.create_list(tenant_id="t2", user_id="u2", title="Чужой список")
    [item], _ = foreign.add_items(list_id=cl.id, items=["Секрет"])
    foreign_id = item.id

    r = tools["delete_checklist_item"].invoke({"item_id": foreign_id})
    assert r == "error: item_not_found", r
    session.expire_all()
    assert session.get(ChecklistItem, foreign_id) is not None, (
        "чужой пункт удалён — нарушена изоляция"
    )


def test_mark_archived_list_item_not_found_no_mutation(tools, session):
    """Codex review R2: пункт в АРХИВНОМ списке → mark по item_id →
    item_not_found, без мутации. active-фильтр зашит в атомарный UPDATE
    («менять можно лишь то, что мог выбрать читающий шаг» — list_checklist_items
    ищет ТОЛЬКО active). Закрывает гонку «архив между lookup и mutate»."""
    from sreda.db.models.checklists import Checklist
    tools["add_checklist_items"].invoke(
        {"list_id_or_title": "Дача", "items": ["Лопата"]}
    )
    svc = ChecklistService(session)
    cl = svc.list_active(tenant_id="t1", user_id="u1")[0]
    target_id = svc.list_items(list_id=cl.id)[0].id
    session.get(Checklist, cl.id).status = "archived"
    session.commit()
    r = tools["mark_checklist_item_done"].invoke({"item_id": target_id})
    assert r == "error: item_not_found", r
    session.expire_all()
    assert session.get(ChecklistItem, target_id).status == "pending", (
        "пункт архивного списка изменён — нарушен mutate-инвариант"
    )


def test_delete_archived_list_item_not_found_no_mutation(tools, session):
    from sreda.db.models.checklists import Checklist
    tools["add_checklist_items"].invoke(
        {"list_id_or_title": "Дача", "items": ["Лопата"]}
    )
    svc = ChecklistService(session)
    cl = svc.list_active(tenant_id="t1", user_id="u1")[0]
    target_id = svc.list_items(list_id=cl.id)[0].id
    session.get(Checklist, cl.id).status = "archived"
    session.commit()
    r = tools["delete_checklist_item"].invoke({"item_id": target_id})
    assert r == "error: item_not_found", r
    session.expire_all()
    assert session.get(ChecklistItem, target_id) is not None, (
        "пункт архивного списка удалён — нарушен mutate-инвариант"
    )


def test_copy_docs_no_legacy_checklist_contract():
    """Codex review R2: docs/copy/checklists.md не должен учить legacy
    mark/delete(list_id_or_title, item_title_match) — иначе дрейф вернётся
    в prompt/доки. Контракт теперь строго item_id."""
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / "docs" / "copy" / "checklists.md"
    text = p.read_text(encoding="utf-8")
    assert "item_title_match" not in text, "legacy item_title_match остался в copy docs"


def test_get_owned_item_rejects_foreign(session):
    """Сервисный геттер: чужой пункт не возвращается, свой — возвращается."""
    svc = ChecklistService(session)
    cl = svc.create_list(tenant_id="t1", user_id="u1", title="Свой")
    [item], _ = svc.add_items(list_id=cl.id, items=["X"])
    assert svc.get_owned_item(item_id=item.id, tenant_id="t1", user_id="u1") is not None
    assert svc.get_owned_item(item_id=item.id, tenant_id="t2", user_id="u2") is None
    assert svc.get_owned_item(item_id="clitem_missing", tenant_id="t1", user_id="u1") is None


# ---------------------------------------------------------------------------
# Презентер #141 — новый Ok-вариант не светит внутренние id
# ---------------------------------------------------------------------------


def test_list_checklist_items_presenter_hides_ids():
    # _DISPLAY_FIELD_MAP — module-level singleton; test_115_* фикстуры
    # оставляют его в {} в teardown (set_display_field_map({})). Чтобы тест не
    # зависел от порядка запуска, явно строим карту из ALL_TOOL_SPECS (в ней
    # есть новый list_checklist_items), затем сбрасываем в None — пусть другие
    # модули лениво пересоберут (паттерн test_compose_humanize_normalizer /
    # test_presenters; см. feedback_pytest_monkeypatch_required).
    from sreda.services.composer import presenters as _p
    from sreda.services.tool_schemas.specs import ALL_TOOL_SPECS
    _p.set_display_field_map(_p.build_display_field_map(ALL_TOOL_SPECS))
    try:
        _HEX = "0" * 24
        model = ListChecklistItemsOk(items=[
            ListChecklistItemsRow(
                item_id=f"clitem_{_HEX}", item_title="Лопата",
                list_title="Дача", list_id=f"checklist_{_HEX}",
                item_status="pending"),
        ])
        rendered = render_display_text(
            "list_checklist_items", model.model_dump(), domain_status=model.status,
        )
        assert not _ID_RE.findall(rendered), f"светит id: {rendered!r}"
        assert rendered not in BREAKDOWN_POOL, "отдал «поломку» вместо данных"
        assert "Лопата" in rendered
    finally:
        _p._DISPLAY_FIELD_MAP = None  # пусть другие модули лениво пересоберут


# ---------------------------------------------------------------------------
# CRITICAL (Codex review #143 B): mark_checklist_item_done → error:item_not_found
# НЕ должен давать unknown_outcome («поломка»/admin-alert). Executor обязан
# НАЙТИ error-ветку и выбрать мягкий id-free compose (критерий приёмки #8
# закрыт end-to-end на уровне исполнения, а не только парсинга).
# ---------------------------------------------------------------------------
import asyncio  # noqa: E402

from sreda.runtime.planner.executor import execute_plan  # noqa: E402
from sreda.runtime.planner.plan_compiler import compile as compile_plan  # noqa: E402
from sreda.runtime.planner.schemas import Plan  # noqa: E402
from sreda.services.tool_schemas.specs import MIGRATED_TOOL_SPECS  # noqa: E402

_REGISTRY = {s.name: s for s in MIGRATED_TOOL_SPECS}


class _FakeMarkTool:
    """LangChain-tool-shaped: возвращает фиксированную сырую строку."""

    def __init__(self, name: str, return_raw: str) -> None:
        self.name = name
        self._raw = return_raw

    async def ainvoke(self, args: dict) -> str:
        return self._raw


def _mark_plan() -> Plan:
    """Одношаговый план: mark_checklist_item_done по item_id с ветками
    done (успех) И error (мягкий id-free compose). Зеркалит few-shot."""
    return Plan.model_validate({
        "schema_version": 1,
        "turn_classification": {"is_new_turn": True, "reason": "test"},
        "clarity": "clear",
        "actions": {
            "s1": {
                "tool": "mark_checklist_item_done",
                "args": {"item_id": "clitem_" + "a" * 24},
                "expected_outcomes": [
                    {"match": {"status": "done"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "checklist_items_show",
                                 "template_data": {"items": []}}},
                    {"match": {"status": "error"}, "next": None,
                     "compose": {"kind": "template",
                                 "template_id": "checklist_items_empty",
                                 "template_data": {}}},
                ],
                "intent_group": "default",
                "depends_on": [],
            },
        },
        "compose": {"kind": "template", "template_id": "checklist_items_empty",
                    "template_data": {}},
    })


def test_mark_item_not_found_matches_error_branch_no_unknown_outcome():
    """error:item_not_found → executor находит error-ветку, НЕ unknown_outcome."""
    plan = _mark_plan()
    ep = compile_plan(plan, _REGISTRY)
    tool = _FakeMarkTool("mark_checklist_item_done", "error: item_not_found")
    log = asyncio.run(execute_plan(
        ep, tools_by_name={"mark_checklist_item_done": tool}, registry=_REGISTRY,
    ))
    step = log.by_step_id()["s1"]
    # ГЛАВНОЕ: НЕ unknown_outcome (это давало бы «поломку»/admin-alert).
    assert step.status != "unknown_outcome", (
        f"error:item_not_found ушёл в unknown_outcome: {step.error_summary!r}"
    )
    assert step.status == "ok", step.status
    # Сматчилась именно error-ветка (вторая, индекс 1) с мягким compose.
    assert step.parsed_output.get("status") == "error"
    assert step.selected_compose is not None
    assert step.selected_compose.template_id == "checklist_items_empty"
    # План в целом НЕ aborted (нет «поломки»).
    assert log.outcome != "aborted", log.outcome


def test_mark_item_not_found_error_code_is_checklist_specific():
    """Голый error:item_not_found → стабильный error_code checklist_item_not_found
    (консистентность с динамической формой и spec-описаниями, MINOR)."""
    from sreda.services.tool_schemas.housewife import parse_tool_output
    parsed = parse_tool_output("mark_checklist_item_done", "error: item_not_found")
    assert getattr(parsed, "status", None) == "error", parsed
    assert getattr(parsed, "error_code", None) == "checklist_item_not_found", parsed
