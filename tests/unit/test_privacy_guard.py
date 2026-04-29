from sreda.services.privacy_guard import get_default_privacy_guard


def test_privacy_guard_redacts_common_sensitive_fragments() -> None:
    guard = get_default_privacy_guard()
    text = (
        "email test@example.com, "
        "номер лицевого счета: 1407009, пароль qwerty, "
        "логин user@example.com, token abc123, "
        "ссылка https://example.com/reset?token=abc123"
    )

    result = guard.sanitize_text(text)

    assert result is not None
    assert result.contains_sensitive_data is True
    assert "[email]" in result.sanitized_text
    assert "[account_number]" in result.sanitized_text
    assert "[password]" in result.sanitized_text
    assert "[login]" in result.sanitized_text
    assert "[secret]" in result.sanitized_text
    assert "[url]" in result.sanitized_text


# 2026-04-29: phone-rule снят (см. privacy_guard.py). Телефоны теперь
# проходят как plaintext в sanitized_text — это нужно для скилов,
# которые сохраняют контакты (аптеки, врачи, члены семьи). Тест
# фиксирует это новое поведение.
def test_privacy_guard_does_not_mask_phone_numbers() -> None:
    guard = get_default_privacy_guard()
    cases = [
        "позвони +79261234567",
        "тел 8 (926) 123-45-67",
        "номер 89261234567 это мой",
        "+7 999 123-45-67",
        "+7 495 969-50-03",
    ]
    for text in cases:
        result = guard.sanitize_text(text)
        assert result is not None, text
        assert "[phone]" not in result.sanitized_text, (
            f"phone unexpectedly masked in {text!r} → {result.sanitized_text!r}"
        )
        assert "[number]" not in result.sanitized_text, (
            f"phone-as-number unexpectedly masked in {text!r} → {result.sanitized_text!r}"
        )


def test_privacy_guard_does_not_redact_plain_claim_id() -> None:
    guard = get_default_privacy_guard()

    result = guard.sanitize_text("Что по заявке 6230173?")

    assert result is not None
    assert result.sanitized_text == "Что по заявке 6230173?"
    assert result.contains_sensitive_data is False


# 2026-04-29 (incident user_tg_1089832184): regex без word-boundary
# матчил 10-значный фрагмент внутри structural ID типа
# `user_tg_1089832184` → заменял на `user_tg_[phone]`. При insert
# outbox это вызывало FK violation (нет такого user_id в users) и
# юзер 1089832184 не получал ответы на /start. После fix'а regex'ы
# имеют explicit `(?<!\w)/(?!\w)` boundaries — underscore блокирует
# match внутри ID'ов.


def test_privacy_guard_preserves_user_tg_id() -> None:
    """Structural ID `user_tg_1089832184` должен пройти без изменений."""
    guard = get_default_privacy_guard()
    for raw_id in [
        "user_tg_1089832184",
        "tenant_tg_1089832184",
        "workspace_tg_352612382",
        "user_tg_634496616",
    ]:
        result = guard.sanitize_text(raw_id)
        assert result is not None
        assert result.sanitized_text == raw_id, (
            f"structural ID {raw_id!r} was mangled to {result.sanitized_text!r}"
        )
        assert result.contains_sensitive_data is False


def test_privacy_guard_preserves_outbox_id() -> None:
    """Outbox/run/job ID типа `out_xxx` или `run_xxx` тоже не должен
    маскироваться."""
    guard = get_default_privacy_guard()
    for raw_id in [
        "out_9a1b511a02d84c66af83910e",
        "run_8b3a91c4f5d647e88fac0029",
        "job_4e2f1d6a7b89c01234567890",
    ]:
        result = guard.sanitize_text(raw_id)
        assert result is not None
        assert result.sanitized_text == raw_id


def test_privacy_guard_structure_passes_through_id_keys() -> None:
    """В nested dict'е поля типа `user_id`, `chat_id`, `tenant_id` не
    должны санитайзиться — это структурные идентификаторы. Только
    контентные поля (text/message/content/items под не-ID ключом)
    sanitize'ятся.

    2026-04-29 (Variant E — belt-and-suspenders к regex-fix'у):
    walker сам прокинет ID нетронутым даже если кто-то в будущем
    добавит regex без word-boundary. Контент ВЛОЖЕННЫХ контейнеров
    под ID-ключом тоже не трогается (но ActionEnvelope не вкладывает
    контент в ID-keys, так что edge case теоретический)."""
    guard = get_default_privacy_guard()
    payload = {
        # Все ID — passthrough
        "user_id": "user_tg_1089832184",
        "chat_id": 1089832184,
        "tenant_id": "tenant_tg_1089832184",
        "workspace_id": "workspace_tg_1089832184",
        "external_chat_id": "1089832184",
        "feature_key": "housewife_assistant",
        "action_type": "chat",
        "channel_type": "telegram",
        "bot_key": "sreda",
        # Контентные поля — sanitize
        "text": "напиши на test@example.com",
        "items": [
            "email: other@example.com",
        ],
        # Nested params dict с контентом — рекурсивный sanitize
        "params": {
            "message_text": "пароль qwerty12",
        },
    }

    result = guard.sanitize_structure(payload)
    s = result.sanitized_value

    # Все ID'ы прошли без изменений
    assert s["user_id"] == "user_tg_1089832184"
    assert s["chat_id"] == 1089832184
    assert s["tenant_id"] == "tenant_tg_1089832184"
    assert s["workspace_id"] == "workspace_tg_1089832184"
    assert s["external_chat_id"] == "1089832184"
    assert s["feature_key"] == "housewife_assistant"
    assert s["action_type"] == "chat"
    assert s["channel_type"] == "telegram"
    assert s["bot_key"] == "sreda"
    # Контентные поля санитайзились
    assert "[email]" in s["text"]
    assert "[email]" in s["items"][0]
    # Вложенный dict под не-ID ключом — рекурсивно sanitize
    assert "[password]" in s["params"]["message_text"]


def test_privacy_guard_sanitizes_nested_structure() -> None:
    guard = get_default_privacy_guard()
    payload = {
        "text": "мой телефон +7 999 123-45-67",
        "items": [
            "email test@example.com",
            {"note": "пароль 123456"},
        ],
    }

    result = guard.sanitize_structure(payload)

    # Phone больше не маскируется, проходит plaintext'ом.
    # Email и password продолжают маскироваться.
    assert result.contains_sensitive_data is True
    assert result.sanitized_value == {
        "text": "мой телефон +7 999 123-45-67",
        "items": [
            "email [email]",
            {"note": "пароль [password]"},
        ],
    }
