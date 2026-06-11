"""#126 п.3 — тона персоны в роте (композере).

Пресеты warm_practical / tender_care исторически читал только
легаси-промпт; рот говорил одним зашитым голосом. Теперь
``ComposerContext.persona_preset`` доносит выбор пользователя:

- ``tender_care`` → к системному промпту добавляется композерная
  накладка тона (НЕ легаси-overlay: тот про инструменты);
- ``warm_practical`` / None → промпт байт-в-байт прежний (нулевой
  риск для всех, кто тон не менял).

Чек-лист принятия называет эти тесты.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sreda.runtime.planner.executor import ExecutionLog, StepResult
from sreda.services.composer.compose import ComposerContext
from sreda.services.composer.llm_composer import make_llm_composer
from sreda.services.composer.llm_prompts_housewife import (
    TENDER_CARE_COMPOSER_OVERLAY,
)
from sreda.services.composer.prompts_registry import LLM_PROMPT_REGISTRY


class _FakeAIMessage:
    def __init__(self, content: str) -> None:
        self.content = content


def _settings() -> Any:
    return SimpleNamespace(
        composer_provider="mimo-flash", composer_timeout_sec=5.0,
    )


def _log() -> ExecutionLog:
    return ExecutionLog(
        steps=(
            StepResult(step_id="s1", tool="add_shopping_items",  # type: ignore[arg-type]
                       status="ok", parsed_output={"status": "added"}),
        ),
        outcome="completed",  # type: ignore[arg-type]
    )


def _capture_invoke(captured: dict):
    def _invoke(runnable: Any, messages: list, *, timeout_seconds: float) -> Any:
        captured["messages"] = messages
        return _FakeAIMessage("ок")
    return _invoke


def _compose_with(preset: str | None) -> str:
    """Прогоняет humanize_result с заданным пресетом, возвращает system text."""
    captured: dict = {}
    composer = make_llm_composer(
        registry=LLM_PROMPT_REGISTRY,
        settings_factory=_settings,
        chat_llm_factory=lambda *, settings, provider: object(),
        invoke=_capture_invoke(captured),
    )
    composer(
        llm_prompt_key="humanize_result",
        template_data={
            "intent": "тест",
            "actions": [{"user_visible_summary": "Записала: x.",
                         "status": "ok"}],
        },
        execution_log=_log(),
        ctx=ComposerContext(
            tenant_id="t", run_id="r", user_message="тест",
            persona_preset=preset,
        ),
    )
    return captured["messages"][0].content


def test_tender_care_appends_composer_overlay() -> None:
    sp = _compose_with("tender_care")
    assert sp.rstrip().endswith(TENDER_CARE_COMPOSER_OVERLAY.strip())
    assert "нежн" in TENDER_CARE_COMPOSER_OVERLAY


def test_overlay_does_not_displace_rules_priority_line() -> None:
    """Накладка идёт ПОСЛЕ строки «правила важнее голоса» (порядок
    проверяется индексами — субагент R1 M-5), и сама оговаривает
    приоритет правил."""
    sp = _compose_with("tender_care")
    prio = sp.find("Если правила и голос конфликтуют")
    overlay = sp.find("Тон: нежная забота")
    assert 0 < prio < overlay, (prio, overlay)
    assert "важнее тона" in TENDER_CARE_COMPOSER_OVERLAY


def test_warm_practical_is_byte_identical_to_default() -> None:
    assert _compose_with("warm_practical") == _compose_with(None)


def test_default_has_no_overlay() -> None:
    assert TENDER_CARE_COMPOSER_OVERLAY.strip() not in _compose_with(None)


def test_legacy_overlay_not_used_in_composer() -> None:
    """Легаси-overlay (про tools/память) НЕ должен попадать в рот."""
    sp = _compose_with("tender_care")
    assert "Не спорь с пользователем о стиле" not in sp
    assert "Сначала делай нужное действие через tools" not in sp


# ---------------------------------------------------------------------------
# Боевой путь planner_chat (субагент R1 MAJOR M-1: fail-soft контракт и
# positive-проброс обязаны иметь именованные тесты уровня хода)
# ---------------------------------------------------------------------------


def test_persona_read_failure_never_kills_turn(monkeypatch, tmp_path) -> None:
    """Сбой чтения пресета (например, БД) → ход выживает, тон None."""
    from pathlib import Path  # noqa: F401 — единый стиль с test_121
    from uuid import uuid4
    from test_121_voice_all_replies import _exec_log, _wire

    import sreda.runtime.planner_chat as pc
    from sreda.services.composer import llm_composer as voice_mod
    from test_120_planner_gate import _run_turn

    def _boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "sreda.services.housewife_persona.get_persona_preset", _boom,
    )

    voice_ctx: list = []

    def fake_voice(**kw):
        voice_ctx.append(kw["ctx"])

        class _V:
            text = "живой ответ"
        return _V()

    _wire(monkeypatch, exec_log=_exec_log())
    monkeypatch.setattr(voice_mod, "DEFAULT_LLM_COMPOSER", fake_voice)
    monkeypatch.setattr(pc, "send_admin_alert", lambda *a, **kw: None)

    session, telegram, _ = _run_turn(
        monkeypatch, tmp_path, f"t1_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        assert telegram.sent, "ход обязан выжить при сбое чтения тона"
        assert voice_ctx and voice_ctx[0].persona_preset is None
    finally:
        session.close()


def test_tender_care_reaches_voice_ctx(monkeypatch, tmp_path) -> None:
    """Positive-проброс: пресет из хранилища доезжает до контекста рта."""
    from uuid import uuid4
    from test_121_voice_all_replies import _exec_log, _wire

    import sreda.runtime.planner_chat as pc
    from sreda.services.composer import llm_composer as voice_mod
    from test_120_planner_gate import _run_turn

    monkeypatch.setattr(
        "sreda.services.housewife_persona.get_persona_preset",
        lambda *a, **kw: "tender_care",
    )

    voice_ctx: list = []

    def fake_voice(**kw):
        voice_ctx.append(kw["ctx"])

        class _V:
            text = "живой ответ"
        return _V()

    _wire(monkeypatch, exec_log=_exec_log())
    monkeypatch.setattr(voice_mod, "DEFAULT_LLM_COMPOSER", fake_voice)
    monkeypatch.setattr(pc, "send_admin_alert", lambda *a, **kw: None)

    session, telegram, _ = _run_turn(
        monkeypatch, tmp_path, f"t2_{uuid4().hex[:6]}.db", gate="t1",
    )
    try:
        assert telegram.sent
        assert voice_ctx and voice_ctx[0].persona_preset == "tender_care"
    finally:
        session.close()
