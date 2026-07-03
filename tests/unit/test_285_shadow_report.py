"""#285 Фаза A (срез A3): тесты shadow-отчёта — сверка ловит расхождения, вывод без ПД."""

from __future__ import annotations

import json
from types import SimpleNamespace

from scripts.analysis_285_shadow_report import compute, render

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


def test_web_scope_clean_on_web_and_meta():
    rows = [_row(_pol(True), [{"name": "web_search", "result_kind": "ok", "observed": True},
                              {"name": "ask_human", "result_kind": "ok", "observed": True}])]
    assert compute(rows)["mismatch_web_scope"] == 0


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
    rows = [
        _row(None, [{"name": "need_family", "result_kind": "ok", "observed": True}],
             confirm_state="confirmed", confirm_resolution="yes", passes=4, outcome="fallback_used"),
        _row(None, [], confirm_resolution="no"),
    ]
    ev = compute(rows)["events"]
    assert ev["confirm_pause"] == 1 and ev["confirm_yes"] == 1 and ev["confirm_no"] == 1
    assert ev["need_family"] == 1 and ev["multi_pass_gt2"] == 1 and ev["outcome_fallback_used"] == 1


def test_output_has_no_user_text():
    rows = [_row(_pol(True), [{"name": "add_task", "result_kind": "ok", "observed": True}]),
            _row(None, [])]
    out = render(compute(rows))
    assert SENTINEL not in out and "tenant_z" not in out
    assert "add_task" in out  # имя инструмента расхождения — допустимый агрегат
