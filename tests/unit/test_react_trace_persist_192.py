"""#192 Фаза B — персист-модуль react_trace_persist: жизненный цикл + идемпотентность + collect.

Хуки в handle_turn — отдельно. Здесь тестируем сами функции start/pause/finish (терминал неизменен,
pause не переоткрывает done, finish-only recovery) + collect_tool_calls (HMAC, merge-by-id, result_kind).
"""
from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from sreda.config import settings as st_mod
from sreda.db.models import ReactTurnTrace


@pytest.fixture()
def persist(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sreda.db.base import Base
    from sreda.runtime import react_trace_persist as p

    engine = create_engine(f"sqlite:///{tmp_path / 'trace.db'}")
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine)
    monkeypatch.setattr(p, "_session", lambda: SF())
    monkeypatch.setenv("SREDA_REACT_TRACE_ENABLED", "1")
    st_mod.get_settings.cache_clear()
    yield p, SF
    st_mod.get_settings.cache_clear()


def _row(SF, turn_key):
    s = SF()
    try:
        return s.query(ReactTurnTrace).filter(ReactTurnTrace.turn_key == turn_key).all()
    finally:
        s.close()


def test_lifecycle_start_pause_finish(persist):
    p, SF = persist
    tk = "react:tg:t1:m1"
    p.persist_trace_start(tenant_id="t1", user_id="u1", thread_id="th", channel="tg",
                          turn_key=tk, origin_user_text="напомни купить молоко")
    rows = _row(SF, tk)
    assert len(rows) == 1 and rows[0].status == "in_progress"

    p.persist_trace_pause(tenant_id="t1", user_id="u1", turn_key=tk)
    r = _row(SF, tk)[0]
    assert r.status == "awaiting_confirm" and r.confirm_state == "pending"

    p.persist_trace_finish(tenant_id="t1", user_id="u1", thread_id="th", channel="tg",
                           turn_key=tk, reply_text="Готово.", llm_calls=[{"call_index": 0}],
                           tool_calls=[{"name": "schedule_reminder", "ok": True}],
                           confirm_state="confirmed", outcome="ok", passes=2)
    r = _row(SF, tk)[0]
    assert r.status == "done" and r.confirm_state == "confirmed" and r.outcome == "ok"
    assert r.reply_text == "Готово." and r.passes == 2
    assert json.loads(r.tool_calls_json)[0]["name"] == "schedule_reminder"
    assert r.origin_user_text == "напомни купить молоко"  # origin не затёрт


def test_abandoned_terminalizes_awaiting_confirm(persist):
    """#320.1: брошенная пауза (redirect/#316 ИЛИ протухание) → awaiting_confirm НЕ висит вечно:
    done + confirm_state=redirected|expired; confirm_resolution=redirect ТОЛЬКО у redirected confirm-паузы
    (ask_human redirect НЕ загрязняет confirm-метрику — R1 субагент)."""
    p, SF = persist
    for reason, is_confirm, want_res in (("redirected", True, "redirect"),
                                         ("redirected", False, None),   # ask_human redirect
                                         ("expired", True, None), ("expired", False, None)):
        tk = f"react:tg:t1:ab-{reason}-{is_confirm}"
        p.persist_trace_start(tenant_id="t1", user_id="u1", thread_id="th", channel="tg",
                              turn_key=tk, origin_user_text="удали задачу X")
        p.persist_trace_pause(tenant_id="t1", user_id="u1", turn_key=tk)
        p.persist_trace_abandoned(tenant_id="t1", user_id="u1", turn_key=tk,
                                  reason=reason, is_confirm=is_confirm)
        r = _row(SF, tk)[0]
        assert r.status == "done" and r.confirm_state == reason, (reason, r.status, r.confirm_state)
        assert r.confirm_resolution == want_res, (reason, is_confirm, r.confirm_resolution)
        assert r.finished_at is not None
        assert r.origin_user_text == "удали задачу X"  # origin не затёрт (лёгкий переход)


def test_abandoned_only_from_awaiting_confirm(persist):
    """#320.1: терминал/живой ход НЕ трогаем — abandoned применяется ТОЛЬКО из awaiting_confirm."""
    p, SF = persist
    # in_progress (живой ход) — не трогаем
    tk1 = "react:tg:t1:ab-live"
    p.persist_trace_start(tenant_id="t1", user_id="u1", thread_id="th", channel="tg",
                          turn_key=tk1, origin_user_text="x")
    p.persist_trace_abandoned(tenant_id="t1", user_id="u1", turn_key=tk1, reason="expired")
    assert _row(SF, tk1)[0].status == "in_progress"
    # done (терминал) — неизменен
    tk2 = "react:tg:t1:ab-done"
    p.persist_trace_start(tenant_id="t1", user_id="u1", thread_id="th", channel="tg",
                          turn_key=tk2, origin_user_text="x")
    p.persist_trace_finish(tenant_id="t1", user_id="u1", thread_id="th", channel="tg",
                           turn_key=tk2, reply_text="ок", llm_calls=[], tool_calls=[],
                           confirm_state="none", outcome="ok", passes=1)
    p.persist_trace_abandoned(tenant_id="t1", user_id="u1", turn_key=tk2, reason="redirected")
    r = _row(SF, tk2)[0]
    assert r.status == "done" and r.confirm_state == "none"  # abandoned не перезаписал финал


def test_finish_stores_routing_decision(persist):
    """#221 Ф3b: persist_trace_finish пишет routing_decision_json; без аргумента (disabled) — NULL."""
    p, SF = persist
    tk = "react:tg:t1:rd"
    rd = json.dumps({"mode": "execute", "primary_domain": "checklists",
                     "allowed_read": ["checklists"], "allowed_write": ["checklists"]}, ensure_ascii=False)
    p.persist_trace_finish(tenant_id="t1", user_id="u1", thread_id="th", channel="tg",
                           turn_key=tk, reply_text="ok", llm_calls=[], tool_calls=[],
                           confirm_state="none", outcome="ok", passes=1, routing_decision_json=rd)
    r = _row(SF, tk)[0]
    assert json.loads(r.routing_decision_json)["primary_domain"] == "checklists"

    tk2 = "react:tg:t1:rd0"  # disabled (аргумент не передан) → NULL
    p.persist_trace_finish(tenant_id="t1", user_id="u1", thread_id="th", channel="tg",
                           turn_key=tk2, reply_text="ok", llm_calls=[], tool_calls=[],
                           confirm_state="none", outcome="ok", passes=1)
    assert _row(SF, tk2)[0].routing_decision_json is None


def test_finish_immutable_after_done(persist):
    """replay finish на уже-done → терминальные поля НЕ меняются (conditional)."""
    p, SF = persist
    tk = "react:tg:t1:done"
    p.persist_trace_start(tenant_id="t1", user_id="u1", thread_id="th", channel="tg",
                          turn_key=tk, origin_user_text="x")
    p.persist_trace_finish(tenant_id="t1", user_id="u1", thread_id="th", channel="tg",
                           turn_key=tk, reply_text="первый", llm_calls=[], tool_calls=[],
                           confirm_state="none", outcome="ok", passes=1)
    # повторный finish с другими данными — не должен перезаписать
    p.persist_trace_finish(tenant_id="t1", user_id="u1", thread_id="th", channel="tg",
                           turn_key=tk, reply_text="ВТОРОЙ", llm_calls=[], tool_calls=[],
                           confirm_state="none", outcome="tool_error", passes=9)
    rows = _row(SF, tk)
    assert len(rows) == 1
    assert rows[0].reply_text == "первый" and rows[0].outcome == "ok" and rows[0].passes == 1


def test_pause_does_not_reopen_done(persist):
    """replay pause на done → НЕ откатывает в awaiting_confirm (conditional WHERE in_progress)."""
    p, SF = persist
    tk = "react:tg:t1:p"
    p.persist_trace_start(tenant_id="t1", user_id="u1", thread_id="th", channel="tg",
                          turn_key=tk, origin_user_text="x")
    p.persist_trace_finish(tenant_id="t1", user_id="u1", thread_id="th", channel="tg",
                           turn_key=tk, reply_text="r", llm_calls=[], tool_calls=[],
                           confirm_state="confirmed", outcome="ok", passes=1)
    p.persist_trace_pause(tenant_id="t1", user_id="u1", turn_key=tk)  # replay исходной pause
    assert _row(SF, tk)[0].status == "done"  # не откатилось


def test_start_idempotent(persist):
    """повтор start (replay) → DO NOTHING, не двоит, origin не трогает."""
    p, SF = persist
    tk = "react:tg:t1:i"
    p.persist_trace_start(tenant_id="t1", user_id="u1", thread_id="th", channel="tg",
                          turn_key=tk, origin_user_text="оригинал")
    p.persist_trace_start(tenant_id="t1", user_id="u1", thread_id="th", channel="tg",
                          turn_key=tk, origin_user_text="ДРУГОЙ")
    rows = _row(SF, tk)
    assert len(rows) == 1 and rows[0].origin_user_text == "оригинал"


def test_finish_only_recovery(persist):
    """finish без start (start потерян) → INSERT done."""
    p, SF = persist
    tk = "react:tg:t1:fo"
    p.persist_trace_finish(tenant_id="t1", user_id="u1", thread_id="th", channel="tg",
                           turn_key=tk, reply_text="r", llm_calls=[], tool_calls=[],
                           confirm_state="none", outcome="ok", passes=1)
    rows = _row(SF, tk)
    assert len(rows) == 1 and rows[0].status == "done"


def test_disabled_flag_no_write(tmp_path, monkeypatch):
    """флаг ВЫКЛ → персист ничего не пишет (откат к #185)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sreda.db.base import Base
    from sreda.runtime import react_trace_persist as p

    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    SF = sessionmaker(bind=engine)
    monkeypatch.setattr(p, "_session", lambda: SF())
    monkeypatch.delenv("SREDA_REACT_TRACE_ENABLED", raising=False)
    st_mod.get_settings.cache_clear()
    try:
        p.persist_trace_start(tenant_id="t1", user_id="u1", thread_id="th", channel="tg",
                              turn_key="k", origin_user_text="x")
        assert _row(SF, "k") == []
    finally:
        st_mod.get_settings.cache_clear()


def test_collect_tool_calls_hmac_and_merge(persist):
    """collect: HMAC (не сырьё), result_kind из artifact, merge-by-id (re-exec не двоит)."""
    p, _ = persist
    msgs = [
        AIMessage(content="", tool_calls=[
            {"name": "cancel_reminder", "args": {"id": "r1"}, "id": "call1"}]),
        ToolMessage(content="ok", tool_call_id="call1", status="success",
                    artifact={"latency_ms": 12, "result_kind": "ok", "error_type": None}),
        # повтор того же call1 (перевыполнение run_tools на resume) — не должен задвоить
        ToolMessage(content="ok", tool_call_id="call1", status="success",
                    artifact={"latency_ms": 9, "result_kind": "ok"}),
    ]
    res = p.collect_tool_calls(msgs, tenant_id="t1")
    assert len(res) == 1  # merge-by-id
    tc = res[0]
    assert tc["name"] == "cancel_reminder" and tc["ok"] is True and tc["result_kind"] == "ok"
    assert tc["args_hash"].startswith("v1:") and "r1" not in tc["args_hash"]  # HMAC, не сырьё


def test_collect_result_kind_variants(persist):
    """result_kind: error/unavailable/unknown_family → ok=false (не успех вслепую)."""
    p, _ = persist
    msgs = [
        AIMessage(content="", tool_calls=[
            {"name": "add_task", "args": {"t": "x"}, "id": "c1"},
            {"name": "ghost_tool", "args": {}, "id": "c2"},
            {"name": "need_family", "args": {"family": "wat"}, "id": "c3"}]),
        ToolMessage(content="err", tool_call_id="c1", status="error",
                    artifact={"result_kind": "error", "error_type": "ValueError"}),
        ToolMessage(content="недоступен", tool_call_id="c2", status="success",
                    artifact={"result_kind": "unavailable"}),
        ToolMessage(content="неизвестна", tool_call_id="c3", status="success",
                    artifact={"result_kind": "unknown_family"}),
    ]
    by = {t["name"]: t for t in p.collect_tool_calls(msgs, tenant_id="t1")}
    assert by["add_task"]["result_kind"] == "error" and by["add_task"]["ok"] is False
    assert by["add_task"]["error_type"] == "ValueError"
    assert by["ghost_tool"]["result_kind"] == "unavailable" and by["ghost_tool"]["ok"] is False
    assert by["need_family"]["result_kind"] == "unknown_family" and by["need_family"]["ok"] is False


def test_nullable_user_dedup(persist):
    """tenant-wide (user_id=None) + один turn_key → одна строка (expression-unique backstop)."""
    p, SF = persist
    tk = "react:tg:t1:nu"
    p.persist_trace_start(tenant_id="t1", user_id=None, thread_id="th", channel="tg",
                          turn_key=tk, origin_user_text="x")
    p.persist_trace_start(tenant_id="t1", user_id=None, thread_id="th", channel="tg",
                          turn_key=tk, origin_user_text="y")
    assert len(_row(SF, tk)) == 1


def test_tenant_scoped(persist):
    """finish одного тенанта не задевает строку другого с тем же turn_key (user-scope)."""
    p, SF = persist
    tk = "react:tg:shared:k"
    p.persist_trace_start(tenant_id="tA", user_id="u", thread_id="th", channel="tg",
                          turn_key=tk, origin_user_text="A")
    p.persist_trace_start(tenant_id="tB", user_id="u", thread_id="th", channel="tg",
                          turn_key=tk, origin_user_text="B")
    p.persist_trace_finish(tenant_id="tA", user_id="u", thread_id="th", channel="tg",
                           turn_key=tk, reply_text="rA", llm_calls=[], tool_calls=[],
                           confirm_state="none", outcome="ok", passes=1)
    s = SF()
    try:
        rows = {r.tenant_id: r for r in s.query(ReactTurnTrace).filter(
            ReactTurnTrace.turn_key == tk).all()}
        assert rows["tA"].status == "done" and rows["tB"].status == "in_progress"  # tB не задет
    finally:
        s.close()


def test_stale_in_progress_detectable(persist):
    """Застрявшие in_progress находятся запросом (ради чего и stateful)."""
    from datetime import datetime, timedelta, timezone

    p, SF = persist
    p.persist_trace_start(tenant_id="t1", user_id="u", thread_id="th", channel="tg",
                          turn_key="react:tg:t1:stuck", origin_user_text="x")
    s = SF()
    try:
        # «застрявшие» = in_progress (на проде фильтр created_at < now-N мин; здесь свежая → ловим по статусу)
        stuck = s.query(ReactTurnTrace).filter(
            ReactTurnTrace.status == "in_progress",
            ReactTurnTrace.created_at < datetime.now(timezone.utc) + timedelta(minutes=1)).all()
        assert len(stuck) == 1
    finally:
        s.close()
