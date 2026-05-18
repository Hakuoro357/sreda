"""R-39: тесты live_phrase_composer."""

from __future__ import annotations

from sreda.agents.live_phrase_composer import (
    ComposerResult,
    compose_live_phrase,
)
from sreda.agents.live_phrase_lock import LockFail


# ─── Хорошие фразы ───────────────────────────────────────────────────


def test_safe_warm_phrase_returned_unchanged() -> None:
    def fake_llm(sys: str, user: str) -> str:
        return "Как раз перед обедом 🌞"

    result = compose_live_phrase(
        user_text="нет, на 14:00 разбудить Катю",
        first_line="Поставила «Разбудить Катю» на сегодня в 14:00",
        invoke_llm=fake_llm,
    )
    assert isinstance(result, ComposerResult)
    assert result.phrase == "Как раз перед обедом 🌞"
    assert result.lock_failure is None


def test_phrase_stripped_of_whitespace() -> None:
    def fake_llm(_s: str, _u: str) -> str:
        return "  Удачи 🍀  \n"

    result = compose_live_phrase(
        user_text="x", first_line="Поставила на 14:00",
        invoke_llm=fake_llm,
    )
    assert result.phrase == "Удачи 🍀"


# ─── Сбои LLM → пустая фраза ─────────────────────────────────────────


def test_llm_returns_none_yields_empty_phrase() -> None:
    def fake_llm(_s: str, _u: str) -> None:
        return None

    result = compose_live_phrase(
        user_text="x", first_line="x",
        invoke_llm=fake_llm,
    )
    assert result.phrase == ""
    assert result.lock_failure is None


def test_llm_returns_empty_string_yields_empty_phrase() -> None:
    def fake_llm(_s: str, _u: str) -> str:
        return ""

    result = compose_live_phrase(
        user_text="x", first_line="x",
        invoke_llm=fake_llm,
    )
    assert result.phrase == ""


def test_llm_exception_yields_empty_phrase() -> None:
    def fake_llm(_s: str, _u: str) -> str:
        raise TimeoutError("simulated 1.5s timeout")

    result = compose_live_phrase(
        user_text="x", first_line="x",
        invoke_llm=fake_llm,
    )
    assert result.phrase == ""


# ─── Замок → пустая фраза + admin alert ──────────────────────────────


def test_claim_verb_blocked_yields_empty_phrase_and_alert() -> None:
    alerts: list[str] = []

    def fake_llm(_s: str, _u: str) -> str:
        return "Поставила всё как надо, не переживай"

    result = compose_live_phrase(
        user_text="x",
        first_line="Поставила «Х» на 14:00",
        invoke_llm=fake_llm,
        admin_alert_fn=alerts.append,
    )
    assert result.phrase == ""
    assert result.lock_failure is not None
    assert isinstance(result.lock_failure, LockFail)
    assert result.lock_failure.reason == "claim_verb_in_phrase"
    assert len(alerts) == 1
    assert "claim_verb_in_phrase" in alerts[0]


def test_too_long_blocked() -> None:
    long_text = "А" * 130

    def fake_llm(_s: str, _u: str) -> str:
        return long_text

    result = compose_live_phrase(
        user_text="x", first_line="first",
        invoke_llm=fake_llm,
    )
    assert result.phrase == ""
    assert result.lock_failure is not None
    assert result.lock_failure.reason == "too_long"


def test_new_entity_blocked_with_admin_alert() -> None:
    alerts: list[str] = []

    def fake_llm(_s: str, _u: str) -> str:
        # LLM упомянула 16:00 хотя в first_line 14:00 — подмена факта
        return "Удачи в 16:00 🌞"

    result = compose_live_phrase(
        user_text="x",
        first_line="Поставила «X» на сегодня в 14:00",
        invoke_llm=fake_llm,
        admin_alert_fn=alerts.append,
    )
    assert result.phrase == ""
    assert result.lock_failure is not None
    assert result.lock_failure.reason == "new_entity_in_phrase"
    assert len(alerts) == 1
    assert "new_entity_in_phrase" in alerts[0]


def test_admin_alert_exception_doesnt_break_composer() -> None:
    def bad_alert(_: str) -> None:
        raise RuntimeError("alert system down")

    def fake_llm(_s: str, _u: str) -> str:
        return "Поставила и забыла"  # → claim verb блок

    result = compose_live_phrase(
        user_text="x", first_line="x",
        invoke_llm=fake_llm,
        admin_alert_fn=bad_alert,
    )
    # Композитор должен вернуть пустую фразу, не упасть
    assert result.phrase == ""


# ─── Подстановки промпта ─────────────────────────────────────────────


def test_system_prompt_contains_constraints() -> None:
    captured: dict = {}

    def fake_llm(sys: str, user: str) -> str:
        captured["sys"] = sys
        captured["user"] = user
        return ""

    compose_live_phrase(
        user_text="нет, поставь на 14:00",
        first_line="Поставила «X» на 14:00",
        journal_summary="cancel: rem_old; schedule: rem_new",
        invoke_llm=fake_llm,
    )
    sys_prompt = captured["sys"]
    user_prompt = captured["user"]
    # Системный промпт диктует ограничения
    assert "≤120 символов" in sys_prompt
    assert "поставила" in sys_prompt.lower()
    # User промпт содержит запрос + первую строку + summary
    assert "нет, поставь на 14:00" in user_prompt
    assert "Поставила «X» на 14:00" in user_prompt
    assert "cancel: rem_old" in user_prompt


def test_journal_summary_optional() -> None:
    captured: dict = {}

    def fake_llm(_s: str, user: str) -> str:
        captured["user"] = user
        return ""

    compose_live_phrase(
        user_text="ok",
        first_line="Готово",
        invoke_llm=fake_llm,
    )
    # Без journal_summary секция «Журнал сделанного» не появляется
    assert "Журнал сделанного" not in captured["user"]
