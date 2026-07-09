"""Правило Бориса 2026-06-11: (1) любой технический отказ пользователю —
живая «поломка» из пула, случайная; (2) каждый показ поломки ЗАПИСАН на
нашей стороне (ERROR-лог + admin-алерт). Чек-лист принятия называет
эти тесты."""
from __future__ import annotations

import logging

from sreda.services.composer.breakdown_messages import (
    BREAKDOWN_POOL,
    breakdown_phrase,
    note_breakdown,
)
from sreda.services.composer.registry import render


def test_pool_is_alive_and_clean() -> None:
    assert len(BREAKDOWN_POOL) >= 5
    for p in BREAKDOWN_POOL:
        assert "чин" in p or "поломк" in p or "разбира" in p  # создатели чинят
        for tech in ("error", "exception", "статус", "код", "template"):
            assert tech not in p.lower()
    assert breakdown_phrase() in BREAKDOWN_POOL


def test_generic_tool_error_renders_pool_phrase() -> None:
    out = render("generic_tool_error", {})
    assert out in BREAKDOWN_POOL


def test_invalid_plan_fallback_renders_pool_phrase() -> None:
    # attempt_count остаётся легальным опциональным ключом
    assert render("invalid_plan_fallback", {"attempt_count": 2}) in BREAKDOWN_POOL
    assert render("invalid_plan_fallback", {}) in BREAKDOWN_POOL


def test_presenter_deny_returns_pool_and_records(monkeypatch, caplog) -> None:
    """Ровно дыра 15:08 (тихий deny): запрет показа — нет display_field/override
    для (tool, status) — теперь = поломка из пула + ERROR-лог + admin-алерт.

    Вход — СИНТЕТИЧЕСКИЙ непокрытый инструмент: он детерминированно попадает в
    ветку deny-by-default при ЛЮБОМ состоянии карты display_field (в т.ч. если
    её обнулил соседний тест) — order-independent. Раньше здесь стоял
    ``list_checklists`` с пустым ``ok``-списком, но #141 (ab071b1, 2026-06-13)
    дал этому инструменту ``display_summary`` — «успешные операции показываются,
    не поломкой»: пустой список = дружелюбное «нет данных», НЕ поломка (см.
    breakdown_messages: «пустые списки — не поломки»), так что тот вход больше
    не бил в deny."""
    alerts: list[tuple] = []
    monkeypatch.setattr(
        "sreda.services.admin_alerts.send_admin_alert",
        lambda *a, **kw: alerts.append((a, kw)),
    )
    from sreda.services.composer.presenters import render_display_text

    caplog.set_level(logging.ERROR,
                     logger="sreda.services.composer.breakdown_messages")
    tool = "__uncovered_tool__"
    out = render_display_text(tool, {"status": "ok"}, domain_status="ok")
    assert out in BREAKDOWN_POOL
    # Алерт обязан прийти ИМЕННО из ветки presenter_deny (дедуп-ключ несёт
    # источник): это доказывает, что фразу дала deny-by-default, а не иной путь
    # пула, и что синтетический инструмент реально непокрыт — иначе deny не
    # сработал бы и этот алерт не пришёл бы (страж от «магической строки»,
    # которая однажды станет настоящим инструментом).
    assert any(
        kw.get("dedupe_key") == f"breakdown:presenter_deny:{tool}:ok"
        for _a, kw in alerts
    ), "показ поломки обязан дать admin-алерт из ветки presenter_deny"
    assert any("ПОЛОМКА" in r.getMessage() for r in caplog.records), (
        "показ поломки обязан остаться в ERROR-логе"
    )


def test_planner_chat_fallbacks_use_pool() -> None:
    from sreda.runtime.planner_chat import _fallback_error, _fallback_invalid

    assert _fallback_error() in BREAKDOWN_POOL
    assert _fallback_invalid() in BREAKDOWN_POOL


def test_note_breakdown_never_raises(monkeypatch) -> None:
    def _boom(*a, **kw):
        raise RuntimeError("alert down")
    monkeypatch.setattr("sreda.services.admin_alerts.send_admin_alert", _boom)
    note_breakdown("test:source", "detail")  # не должен поднять


def test_branch_compose_breakdown_recorded_and_not_voiced(monkeypatch, tmp_path) -> None:
    """Субагент R1 MAJOR (срез 8): плановая ветка ошибки (generic_tool_error
    терминальной веткой) для конвейера «успех» — fallback_used пуст. Показ
    поломки обязан фиксироваться по членству текста в пуле, и поломка НЕ
    должна прихорашиваться ртом как успешное действие."""
    from uuid import uuid4
    from test_121_voice_all_replies import _exec_log, _wire

    import sreda.runtime.planner_chat as pc
    from sreda.services.composer import llm_composer as voice_mod
    from test_120_planner_gate import _run_turn

    pool_text = BREAKDOWN_POOL[0]
    _wire(monkeypatch, exec_log=_exec_log(), compose_result_text=pool_text)

    voice_calls: list = []

    def fake_voice(**kw):
        voice_calls.append(kw)

        class _V:
            text = "прихорашенный текст"
        return _V()

    monkeypatch.setattr(voice_mod, "DEFAULT_LLM_COMPOSER", fake_voice)
    monkeypatch.setattr(pc, "send_admin_alert", lambda *a, **kw: None)

    noted: list = []
    monkeypatch.setattr(
        "sreda.services.composer.breakdown_messages.note_breakdown",
        lambda source, detail="": noted.append(source),
    )

    session, telegram, _ = _run_turn(
        monkeypatch, tmp_path, f"bp_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        assert telegram.sent and telegram.sent[0]["text"] == pool_text, (
            "поломка уходит пользователю КАК ЕСТЬ (не через рот)"
        )
        assert not voice_calls, "поломку нельзя отдавать рту"
        assert noted and noted[0].startswith("branch_compose:"), noted
    finally:
        session.close()


def test_invalid_plan_path_logs_breakdown(monkeypatch, tmp_path, caplog) -> None:
    """Codex R2 high MAJOR: ранний путь (план невалиден ДО сборки) тоже
    обязан оставлять ERROR-след «ПОЛОМКА» (алерт там уже есть — _alert)."""
    import logging
    from uuid import uuid4
    import sreda.runtime.planner_chat as pc
    import sreda.runtime.planner.orchestrator as orch
    from test_120_planner_gate import _run_turn

    class _Failed:
        success = False
        final_attempt_no = 2
        error_summary = "invalid_plan_after_retry"
        execution_plan = None
        plan = None

    async def fake_orch(ctx, **kw):
        return _Failed()

    monkeypatch.setattr(orch, "run", fake_orch)
    monkeypatch.setattr(pc, "send_admin_alert", lambda *a, **kw: None)
    caplog.set_level(logging.ERROR,
                     logger="sreda.services.composer.breakdown_messages")

    session, telegram, _ = _run_turn(
        monkeypatch, tmp_path, f"ip_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        assert telegram.sent and telegram.sent[0]["text"], "ответ обязан уйти"
        assert any("ПОЛОМКА" in r.getMessage() for r in caplog.records), (
            "ранний путь обязан оставить ERROR-след «ПОЛОМКА»"
        )
    finally:
        session.close()
