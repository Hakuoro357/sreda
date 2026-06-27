from __future__ import annotations

from sreda.runtime.handlers import build_system_prompt
from sreda.services.housewife_persona import (
    PERSONA_TENDER_CARE,
)


def test_housewife_prompt_includes_selected_persona_overlay() -> None:
    prompt = build_system_prompt(
        "housewife_assistant",
        persona_preset=PERSONA_TENDER_CARE,
    )

    assert "PERSONA PRESET: tender_care" in prompt
    assert "солнышко" in prompt
    assert prompt.index("PERSONA PRESET: tender_care") < prompt.index(
        "строгая дисциплина tool-calls"
    )


def test_housewife_prompt_defaults_to_warm_persona_overlay() -> None:
    prompt = build_system_prompt("housewife_assistant")

    assert "PERSONA PRESET: warm_practical" in prompt
    assert "PERSONA PRESET: tender_care" not in prompt


def test_non_housewife_prompt_does_not_include_persona_overlay() -> None:
    prompt = build_system_prompt(None, persona_preset=PERSONA_TENDER_CARE)

    assert "PERSONA PRESET:" not in prompt


def test_tender_care_overlay_conditions_feminine_address() -> None:
    # #242 R1 MAJOR: женские обращения «дорогая»/«моя хорошая» к пользователю
    # разрешены ТОЛЬКО при явно известном женском поле — иначе мисгендеринг
    # мужчины/неизвестного (конфликт с правилом рода пользователя).
    from sreda.services.housewife_persona import build_persona_overlay

    overlay = build_persona_overlay(PERSONA_TENDER_CARE)
    assert "солнышко" in overlay                    # нейтральное — всегда ок
    assert "ЯВНО известен женский" in overlay       # женские формы — под условием
    assert "правило рода пользователя" in overlay
    # старого БЕЗУСЛОВНОГО буллета быть не должно (R2 Codex MINOR)
    assert 'обращаться: «солнышко», «дорогая»' not in overlay


def test_react_system_prompt_threads_persona_overlay_in_tail() -> None:
    # #242: persona-preset overlay прокидывается в ReAct-промт и стоит в ХВОСТЕ
    # (после rules) вместе с context — стабильный префикс кэшируется.
    from sreda.runtime.react_loop import _system_prompt
    from sreda.services.housewife_persona import build_persona_overlay

    today = "2026-06-26 (пятница)"
    base = _system_prompt(today)
    tc = _system_prompt(today, build_persona_overlay(PERSONA_TENDER_CARE))

    assert "<style_preset>" not in base                       # нет preset → нет блока
    assert "<style_preset>" in tc and "tender_care" in tc
    assert tc.index("</rules>") < tc.index("<style_preset>") < tc.index("<context>")
    assert base.rindex("<context>") > base.rindex("</rules>")  # today в хвосте


def test_chat_fact_prompt_carries_persona_gender_and_overlay() -> None:
    # #250: chat/fact-промт (болтовня) несёт ЛИЧНОСТЬ Среды — само-определение,
    # женский род, тон — И overlay стиля. Иначе в болтовне Среда сухая и без рода.
    from sreda.runtime.react_preflight import chat_fact_system_prompt
    from sreda.services.housewife_persona import build_persona_overlay

    today = "2026-06-27 (суббота)"
    base = chat_fact_system_prompt(today)
    assert "ОНА" in base and "-ла/-лась" in base            # о себе женский род
    assert "близкий человек семьи" in base                  # не справочное бюро
    assert today in base
    assert "web_search" in base                             # scoped web-only правила целы
    assert "PERSONA PRESET" not in base                     # без overlay — нет блока

    tc = chat_fact_system_prompt(today, persona_overlay=build_persona_overlay(PERSONA_TENDER_CARE))
    assert "PERSONA PRESET: tender_care" in tc              # overlay прокинут
    assert "ОНА" in tc and "web_search" in tc               # личность + scope сохранены


def test_persona_overlay_for_fail_open_without_session() -> None:
    # #250: helper fail-open — нет сессии → "" (базовый характер), ход не падает.
    from sreda.runtime.react_loop import _persona_overlay_for

    assert _persona_overlay_for(None, "t", "u") == ""
