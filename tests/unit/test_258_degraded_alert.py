# -*- coding: utf-8 -*-
"""#258 — детектор деградировавших ReAct-ходов → алерт оператору.

Пинит: на сбойный исход (штопор max_steps / запасной LLM / ошибка инструмента / safe-reply)
шлётся ровно один send_admin_alert с правильной причиной и ключом дедупа; нормальный ход —
без алерта (нет шума). Сам send_admin_alert (DB-dedup/burst-cap) — мокаем.
"""
from __future__ import annotations


def _patch_alert(monkeypatch):
    calls: list[dict] = []

    def _fake_send(*, severity, title, body, dedupe_key, **kw):  # noqa: ANN001
        calls.append({"severity": severity, "title": title,
                      "body": body, "dedupe_key": dedupe_key})

    monkeypatch.setattr(
        "sreda.services.admin_alerts.send_admin_alert", _fake_send, raising=False)
    return calls


def test_max_steps_detected_by_stop_reply(monkeypatch):
    # Штопор детектится по ТЕКСТУ stop-узла (outcome пишется как «ok») — надёжный признак
    # реального захода в stop, а не косвенный passes.
    from sreda.runtime import react_loop

    calls = _patch_alert(monkeypatch)
    react_loop._maybe_alert_degraded_turn(
        tenant_id="tenant_tg_1", user_id="user_tg_1", channel="telegram",
        turn_key="tk-abc", user_text="сарафан лизе",
        reply_text=react_loop._MAX_STEPS_REPLY,  # терминал stop-узла (содержит маркер)
        outcome="ok", passes=react_loop._MAX_TURN_PASSES)
    assert len(calls) == 1
    assert calls[0]["severity"] == "P2"  # валидный контракт severity (не "warning")
    assert "max_steps" in calls[0]["title"]
    assert calls[0]["dedupe_key"] == "degraded:max_steps:tenant_tg_1"
    assert "tenant_tg_1" in calls[0]["body"] and "tk-abc" in calls[0]["body"]


def test_long_successful_turn_no_false_max_steps(monkeypatch):
    # R1 MAJOR (#258, субагент): успешный длинный ход ровно из _MAX_TURN_PASSES проходов
    # (детуры need_family/guard → passes=лимит, но ответ в END без stop) НЕ должен ложно
    # алертить max_steps — детект по тексту stop, а не по passes.
    from sreda.runtime import react_loop

    calls = _patch_alert(monkeypatch)
    react_loop._maybe_alert_degraded_turn(
        tenant_id="t", user_id="u", channel="telegram", turn_key="tk",
        user_text="сложный составной запрос", reply_text="Готово, всё сделала 👍",
        outcome="ok", passes=react_loop._MAX_TURN_PASSES)
    assert calls == []  # реальный ответ (не текст stop) → не штопор


def test_degraded_outcomes_fire_alert(monkeypatch):
    from sreda.runtime import react_loop

    for outcome in ("fallback_used", "tool_error", "safe_reply"):
        calls = _patch_alert(monkeypatch)
        react_loop._maybe_alert_degraded_turn(
            tenant_id="t", user_id="u", channel="telegram", turn_key="tk",
            user_text="q", reply_text="a", outcome=outcome, passes=1)
        assert len(calls) == 1, outcome
        assert outcome in calls[0]["title"]
        assert calls[0]["dedupe_key"] == f"degraded:{outcome}:t"


def test_normal_turn_no_alert(monkeypatch):
    # Нормальный ход (outcome ok, passes < лимита) → НЕ шумим.
    from sreda.runtime import react_loop

    calls = _patch_alert(monkeypatch)
    react_loop._maybe_alert_degraded_turn(
        tenant_id="t", user_id="u", channel="telegram", turn_key="tk",
        user_text="q", reply_text="a", outcome="ok", passes=2)
    assert calls == []


def test_alert_failure_does_not_raise(monkeypatch):
    # Сбой send_admin_alert НЕ валит ход (best-effort).
    from sreda.runtime import react_loop

    def _boom(**kw):  # noqa: ANN001
        raise RuntimeError("alert backend down")

    monkeypatch.setattr(
        "sreda.services.admin_alerts.send_admin_alert", _boom, raising=False)
    # не должно бросить
    react_loop._maybe_alert_degraded_turn(
        tenant_id="t", user_id="u", channel="telegram", turn_key="tk",
        user_text="q", reply_text="a", outcome="safe_reply", passes=0)
