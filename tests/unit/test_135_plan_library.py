"""#135 срез 1 — чеклист приёмки (issue-коммент 2026-06-12)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.plan_library import PlanLibraryEntry
from sreda.runtime.planner.plan_library import (
    ARG_PLACEHOLDER,
    build_form_tags,
    cleanup_plan_library,
    project_plan_form,
)


def _plan(args=None, title="Купить лекарство от давления для мамы"):
    oc = SimpleNamespace(
        match={"status": "ok"}, next=None,
        compose=SimpleNamespace(kind="template",
                                template_id="checklists_list_show",
                                llm_prompt_key=None))
    a = SimpleNamespace(tool="list_checklists",
                        args=args or {"query": title, "limit": 5},
                        expected_outcomes=[oc], intent_group="default",
                        depends_on=[])
    return SimpleNamespace(clarity="clear", actions={"s1": a},
                           compose=oc.compose)


def test_form_projection_no_pii() -> None:
    """Чеклист п.2: ни один пользовательский литерал не проходит в форму
    (плейсхолдеры); ссылки ${sN.field} — остаются."""
    plan = _plan(args={"query": "Лаванда 298 ТС, простыня 141×200",
                       "items": ["позвонить маме", "${s1.items}"],
                       "n": 7, "flag": True})
    form = project_plan_form(plan)
    blob = json.dumps(form, ensure_ascii=False)
    for leak in ("Лаванда", "маме", "простыня", "141", "298", '"7"', " 7"):
        assert leak not in blob, f"утечка литерала: {leak}"
    shape = form["steps"][0]["args_shape"]
    assert shape["query"] == ARG_PLACEHOLDER
    assert shape["items"] == [ARG_PLACEHOLDER, "${s1.items}"]
    assert shape["n"] == ARG_PLACEHOLDER
    assert shape["flag"] is True  # тип-сигнал, не PII
    # вход эмбеддера = form_json + form_tags — тоже чистый
    tags_blob = json.dumps(build_form_tags(form), ensure_ascii=False)
    assert "Лаванда" not in tags_blob and "маме" not in tags_blob
    import re
    assert not re.search(r"[а-яА-Я]", blob + tags_blob), (
        "кириллица (пользовательский текст) во входе эмбеддера"
    )


def test_form_tags_allowlist_only() -> None:
    form = project_plan_form(_plan())
    tags = build_form_tags(form)
    assert "single" in tags and "list_checklists" in tags
    allowed = {"single", "chain", "empty", "branching", "dependent",
               "clarify", "list_checklists"}
    assert set(tags) <= allowed


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    try:
        yield sess
    finally:
        sess.close()


def _entry(sess, eid, status, days_old, run_id=None):
    sess.add(PlanLibraryEntry(
        id=eid, tenant_id="t1", run_id=run_id, status=status,
        form_json="{}", form_tags="[]", outcome_json="{}",
        created_at=datetime.now(timezone.utc) - timedelta(days=days_old),
    ))


def test_plan_library_retention(session) -> None:
    """Чеклист п.6: TTL всем статусам (90д кандидаты/анти, 180д approved)."""
    _entry(session, "e1", "candidate", 91)
    _entry(session, "e2", "candidate", 10)
    _entry(session, "e3", "anti", 100)
    _entry(session, "e4", "approved", 100)   # живёт (до 180)
    _entry(session, "e5", "approved", 181)   # чистится
    session.commit()
    n = cleanup_plan_library(session)
    session.commit()
    left = {e.id for e in session.query(PlanLibraryEntry).all()}
    assert left == {"e2", "e4"} and n == 3


def test_candidate_recorded_idempotent(session, monkeypatch) -> None:
    """Чеклист п.2: идемпотентность по run_id (повтор не плодит дубль)."""
    from sreda.runtime.planner import plan_library as pl
    monkeypatch.setattr(
        "sreda.db.session.get_session_factory", lambda: (lambda: session))
    # сессию закрывает _record_sync — глушим close, она нужна тесту дальше
    monkeypatch.setattr(session, "close", lambda: None)
    form = project_plan_form(_plan())
    pl._record_sync("t1", "run_x", form, ["single"])
    pl._record_sync("t1", "run_x", form, ["single"])
    rows = session.query(PlanLibraryEntry).all()
    assert len(rows) == 1 and rows[0].status == "candidate"


def test_shadow_selects_only_approved(session, monkeypatch) -> None:
    """Чеклист п.5 (ядро тени): кандидаты НЕ подмешиваются, только
    approved с пересечением тегов; собственный run исключён."""
    from sreda.runtime.planner.plan_library import _shadow_select
    _entry(session, "a1", "approved", 5)
    session.query(PlanLibraryEntry).filter_by(id="a1").update(
        {"form_tags": json.dumps(["single", "list_checklists"])})
    _entry(session, "c1", "candidate", 5)
    session.commit()
    out = _shadow_select(session, "t1",
                         {"steps": []}, ["single", "list_checklists"],
                         exclude_run_id="run_now")
    assert out["would_mix"] == ["a1"]


def test_gate_fail_closed(monkeypatch) -> None:
    """Гейт: пустой/сломанный settings → выключено (fail-closed)."""
    from sreda.runtime.planner.plan_library import is_library_enabled
    assert is_library_enabled("tenant_anything") is False
