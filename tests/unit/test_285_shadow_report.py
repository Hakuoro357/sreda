"""#285 Фаза A (срез A3): тесты shadow-отчёта — сверка ловит расхождения, вывод без ПД."""

from __future__ import annotations

import json
from types import SimpleNamespace

from scripts.analysis_285_shadow_report import compute, exit_status, render

SENTINEL = "ПРИВАТНЫЙ_ТЕКСТ_98765"


def _row(policy: dict | None, tools: list[dict], confirm_state="none",
         confirm_resolution=None, passes=1, outcome="ok"):
    return SimpleNamespace(
        status="done",
        origin_user_text=SENTINEL,
        turn_policy_json=(json.dumps(policy) if policy else None),
        tool_calls_json=json.dumps(tools),
        confirm_state=confirm_state, confirm_resolution=confirm_resolution,
        passes=passes, outcome=outcome, created_at=None, tenant_id="tenant_z",
    )


def _pol(web_only: bool, aw=None):
    return {"v": 1, "prompt_variant": "chat_fact" if web_only else "task",
            "web_scope_only": web_only, "allowed_write_domains": aw}


def test_web_scope_mismatch_detected():
    """chat/fact-полиси + исполненный productivity-инструмент = расхождение (не тавтология:
    полиси со старта хода, исполнение — вся динамика)."""
    rows = [_row(_pol(True), [{"name": "add_task", "result_kind": "ok", "observed": True}])]
    agg = compute(rows)
    assert agg["mismatch_web_scope"] == 1 and agg["mismatch_tools"]["add_task"] == 1


def test_web_scope_clean_on_web_only():
    rows = [_row(_pol(True), [{"name": "web_search", "result_kind": "ok", "observed": True}])]
    assert compute(rows)["mismatch_web_scope"] == 0


def test_web_scope_meta_not_excused():
    """Мета НЕ извиняется (R1 MAJOR CodexM+субагент): сплит на chat/fact мету не биндит вовсе —
    её ok-исполнение = дыра, отчёт обязан её показать (особенно delete_my_account)."""
    rows = [_row(_pol(True), [{"name": "delete_my_account", "result_kind": "ok", "observed": True}]),
            _row(_pol(True), [{"name": "ask_human", "result_kind": "ok", "observed": True}])]
    agg = compute(rows)
    assert agg["mismatch_web_scope"] == 2
    assert agg["mismatch_tools"]["delete_my_account"] == 1


def test_write_domain_mismatch_detected():
    """task-полиси с allowed_write=[tasks] + исполненный write чужого домена = расхождение."""
    rows = [_row(_pol(False, aw=["tasks"]),
                 [{"name": "add_shopping_items", "result_kind": "ok", "observed": True}])]
    agg = compute(rows)
    assert agg["mismatch_write_domain"] == 1


def test_write_domain_none_means_no_check():
    """allowed_write=None (фильтра не было) → любые write без претензий (легаси full-bind)."""
    rows = [_row(_pol(False, aw=None),
                 [{"name": "add_shopping_items", "result_kind": "ok", "observed": True}])]
    assert compute(rows)["mismatch_write_domain"] == 0


def test_unobserved_execution_not_counted():
    """observed=False (rk-дефолт «ok» без ToolMessage) НЕ считается исполнением для сверки."""
    rows = [_row(_pol(True), [{"name": "add_task", "result_kind": "ok", "observed": False}])]
    assert compute(rows)["mismatch_web_scope"] == 0


def test_event_class_counters():
    """Каунты выхода фазы: confirm по resolution (yes|no|redirect), guard/resume из turn_events
    полиси, need_family, result_kind-классы, orphan-счёт (R1 CodexH M2/M4 + субагент m7)."""
    pol = _pol(False, aw=None)
    pol["turn_events"] = {"resumed": True, "guard_attempted": 2, "guard_full": True, "passes": 4}
    rows = [
        _row(pol, [{"name": "need_family", "result_kind": "domain_blocked", "observed": True},
                   {"name": "cancel_task", "result_kind": "ok", "observed": True, "orphan": True}],
             confirm_state="confirmed", confirm_resolution="yes", outcome="fallback_used"),
        _row(None, [], confirm_resolution="no"),
        _row(None, [], confirm_resolution="redirect"),
    ]
    agg = compute(rows)
    ev = agg["events"]
    assert ev["confirm_yes"] == 1 and ev["confirm_no"] == 1 and ev["confirm_redirect"] == 1
    assert ev["need_family"] == 1 and ev["outcome_fallback_used"] == 1
    assert ev["resumed"] == 1 and ev["guard_attempted"] == 1 and ev["guard_full"] == 1
    assert agg["result_kinds"]["domain_blocked"] == 1 and agg["result_kinds"]["ok"] == 1
    assert agg["orphan_records"] == 1 and agg["confirm_rows"] == 1


def test_fail_line_when_no_policy_rows():
    """with_policy==0 → FAIL-строка (окно неверно / shadow не работает) — CodexM M2."""
    out = render(compute([_row(None, [])]))
    assert "FAIL" in out


def test_output_has_no_user_text():
    rows = [_row(_pol(True), [{"name": "add_task", "result_kind": "ok", "observed": True}]),
            _row(None, [])]
    out = render(compute(rows))
    assert SENTINEL not in out and "tenant_z" not in out
    assert "add_task" in out  # имя инструмента расхождения — допустимый агрегат


def test_exit_status_gate(monkeypatch):
    """R2 CodexH MAJOR: код возврата != 0 при with_policy==0 ИЛИ расхождениях (иначе CI пропустит FAIL)."""
    # чистый ход с полиси, без расхождений → 0
    ok = compute([_row(_pol(True), [{"name": "web_search", "result_kind": "ok", "observed": True}])])
    assert exit_status(ok) == 0
    # ни одной строки с полиси (окно неверно / shadow не работает) → 1
    assert exit_status(compute([_row(None, [])])) == 1
    # расхождение web-scope → 1
    bad = compute([_row(_pol(True), [{"name": "add_task", "result_kind": "ok", "observed": True}])])
    assert exit_status(bad) == 1
