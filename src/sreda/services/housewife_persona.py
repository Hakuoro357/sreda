from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from sreda.db.repositories.user_profile import UserProfileRepository

HOUSEWIFE_FEATURE_KEY = "housewife_assistant"

PERSONA_WARM_PRACTICAL = "warm_practical"
PERSONA_TENDER_CARE = "tender_care"
DEFAULT_PERSONA_PRESET = PERSONA_WARM_PRACTICAL
VALID_PERSONA_PRESETS = frozenset({
    PERSONA_WARM_PRACTICAL,
    PERSONA_TENDER_CARE,
})

PERSONA_PRESET_PARAM = "persona_preset"
PERSONA_SELECTED_AT_PARAM = "persona_selected_at"
PERSONA_SOURCE_PARAM = "persona_source"
PERSONA_READY_CALLBACK = "persona_ready"

_PERSONA_SETTINGS_PHRASES = (
    "поменяй стиль общения",
    "настрой стиль общения",
    "сменить стиль общения",
    "выбрать стиль общения",
    "поменять личность",
    "сменить личность",
    "выбрать личность",
)


def normalize_persona_preset(value: str | None) -> str:
    preset = (value or "").strip()
    if preset in VALID_PERSONA_PRESETS:
        return preset
    return DEFAULT_PERSONA_PRESET


def is_persona_settings_request(text: str | None) -> bool:
    normalized = " ".join((text or "").replace("ё", "е").lower().split())
    if not normalized:
        return False
    return any(phrase in normalized for phrase in _PERSONA_SETTINGS_PHRASES)


def get_persona_preset(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
) -> str:
    repo = UserProfileRepository(session)
    config = repo.get_skill_config(tenant_id, user_id, HOUSEWIFE_FEATURE_KEY)
    if config is None:
        return DEFAULT_PERSONA_PRESET
    params = UserProfileRepository.decode_skill_params(config)
    raw = params.get(PERSONA_PRESET_PARAM)
    return normalize_persona_preset(raw if isinstance(raw, str) else None)


def set_persona_preset(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    preset: str,
    source: str = "user_command",
    actor_user_id: str | None = None,
) -> str:
    if preset not in VALID_PERSONA_PRESETS:
        raise ValueError(f"unknown housewife persona preset: {preset!r}")

    repo = UserProfileRepository(session)
    config = repo.get_skill_config(tenant_id, user_id, HOUSEWIFE_FEATURE_KEY)
    params: dict[str, Any] = {}
    if config is not None:
        params.update(UserProfileRepository.decode_skill_params(config))

    params[PERSONA_PRESET_PARAM] = preset
    params[PERSONA_SELECTED_AT_PARAM] = (
        datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    params[PERSONA_SOURCE_PARAM] = source

    repo.upsert_skill_config(
        tenant_id,
        user_id,
        HOUSEWIFE_FEATURE_KEY,
        source=source,
        actor_user_id=actor_user_id,
        skill_params=params,
    )
    return preset


def build_persona_overlay(preset: str | None) -> str:
    normalized = normalize_persona_preset(preset)
    if normalized == PERSONA_TENDER_CARE:
        return """\
PERSONA PRESET: tender_care

Это overlay к одному базовому характеру Среды. Не меняй правила tools,
памяти, безопасности и честности.

Стиль: очень ласковая заботливая помощница для семьи.
- Можно мягко обращаться нейтрально: «солнышко». Женские формы «дорогая»,
  «моя хорошая» — ТОЛЬКО если пол пользователя ЯВНО известен женский; пол
  неизвестен или мужской → нейтральные обращения (правило рода пользователя
  из базового характера ВЫШЕ этого overlay).
- Можно больше тепла, поддержки и бытовой заботы.
- Не превращай ответ в длинную эмоциональную речь.
- Не сюсюкай в задачах, где нужна точность: деньги, здоровье,
  расписание, ошибки, юридические/важные решения.
- Даже в ласковом стиле сначала делай нужное действие через tools,
  потом отвечай.
"""

    return """\
PERSONA PRESET: warm_practical

Это overlay к одному базовому характеру Среды. Не меняй правила tools,
памяти, безопасности и честности.

Стиль: тёплая, спокойная, заботливая помощница для семьи.
- Пиши коротко, по делу, но не сухо.
- Можно быть мягкой и внимательной, без чрезмерной эмоциональности.
- Не используй уменьшительно-ласкательные обращения.
- Не спорь с пользователем о стиле, просто подстраивай тон.
- Сначала делай нужное действие через tools, потом отвечай.
"""


def build_persona_choice_message() -> str:
    return (
        "Я Среда — AI помощница для семьи!\n\n"
        "Помогаю с бытовой рутиной:\n"
        "• 🎙 понимаю голос — можешь говорить или писать сообщения;\n"
        "• ⏰ ставлю напоминания и задачи в расписание;\n"
        "• 🛒 веду список покупок и отмечаю купленное;\n"
        "• 📋 веду списки твоих дел;\n"
        "• 🍽 составляю меню на неделю и собираю продукты к нему;\n"
        "• 📖 сохраняю и нахожу рецепты;\n"
        "• 👨‍👩‍👧 запоминаю важное про семью: состав, привычки, диеты, аллергии;\n"
        "• 🌤 могу подсказать погоду;\n"
        "• 🔍 ищу в интернете нужную тебе информацию.\n\n"
        "Выбери, как мне с тобой общаться:"
    )


# #130 (Codex R2 medium): у колбэка persona: ДВА источника — онбординг-
# приветствие и команда настроек в середине жизни; статус онбординга их
# не различает (post-approve welcome шлёт тот же колбэк при complete).
# Честный дискриминатор — отдельный префикс у клавиатуры настроек.
PERSONA_SETTINGS_CALLBACK_PREFIX = "personaset:"


def build_persona_choice_keyboard_tg(*, settings: bool = False) -> dict:
    prefix = PERSONA_SETTINGS_CALLBACK_PREFIX if settings else "persona:"
    return {
        "inline_keyboard": [[
            {
                "text": "Спокойно и по делу",
                "callback_data": f"{prefix}{PERSONA_WARM_PRACTICAL}",
            },
            {
                "text": "Ласково и тепло",
                "callback_data": f"{prefix}{PERSONA_TENDER_CARE}",
            },
        ]]
    }


def build_persona_choice_keyboard_max(*, settings: bool = False) -> list[dict]:
    prefix = PERSONA_SETTINGS_CALLBACK_PREFIX if settings else "persona:"
    return [{
        "type": "inline_keyboard",
        "payload": {
            "buttons": [[
                {
                    "type": "callback",
                    "text": "Спокойно и по делу",
                    "payload": f"{prefix}{PERSONA_WARM_PRACTICAL}",
                },
                {
                    "type": "callback",
                    "text": "Ласково и тепло",
                    "payload": f"{prefix}{PERSONA_TENDER_CARE}",
                },
            ]]
        },
    }]


def build_persona_selected_message(
    preset: str | None, *, in_onboarding: bool = True
) -> str:
    """#130: текст после выбора стиля зависит от контекста. Онбординг —
    прежний хвост с приглашением к примерам; смена стиля В СЕРЕДИНЕ
    жизни — короткое подтверждение без «давай познакомимся» (прецедент
    Бориса 2026-06-11: команда «поменяй стиль общения» уводила в конец
    онбординга)."""
    normalized = normalize_persona_preset(preset)
    if not in_onboarding:
        if normalized == PERSONA_TENDER_CARE:
            return "Готово — теперь говорю ласково и тепло 😊"
        return "Готово — теперь спокойно и по делу 👌"
    if normalized == PERSONA_TENDER_CARE:
        return (
            "Выбрала ласковый и тёплый стиль.\n\n"
            "Дальше можно сразу дать мне задачу — текстом или голосом.\n"
            "А можно сначала посмотреть пару примеров, чем я могу помочь."
        )
    return (
        "Выбрала спокойный стиль.\n\n"
        "Дальше можно сразу дать мне задачу — текстом или голосом.\n"
        "А можно сначала посмотреть пару примеров, чем я могу помочь."
    )



def build_persona_ready_message() -> str:
    return "Хорошо. Пиши или говори голосом, что нужно сделать."


def build_persona_selected_keyboard_tg() -> dict:
    return {
        "inline_keyboard": [[
            {
                "text": "Покажи примеры",
                "callback_data": "pb:voice",
            },
            {
                "text": "Давай сразу",
                "callback_data": PERSONA_READY_CALLBACK,
            }
        ]]
    }


def build_persona_selected_keyboard_max() -> list[dict]:
    return [{
        "type": "inline_keyboard",
        "payload": {
            "buttons": [[
                {
                    "type": "callback",
                    "text": "Покажи примеры",
                    "payload": "pb:voice",
                },
                {
                    "type": "callback",
                    "text": "Давай сразу",
                    "payload": PERSONA_READY_CALLBACK,
                }
            ]]
        },
    }]
