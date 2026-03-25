from sreda.services.privacy_guard import get_default_privacy_guard


def test_privacy_guard_redacts_common_sensitive_fragments() -> None:
    guard = get_default_privacy_guard()
    text = (
        "Телефон +7 999 123-45-67, email test@example.com, "
        "номер лицевого счета: 1407009, пароль qwerty, "
        "логин user@example.com, token abc123, "
        "ссылка https://example.com/reset?token=abc123"
    )

    result = guard.sanitize_text(text)

    assert result is not None
    assert result.contains_sensitive_data is True
    assert "[phone]" in result.sanitized_text
    assert "[email]" in result.sanitized_text
    assert "[account_number]" in result.sanitized_text
    assert "[password]" in result.sanitized_text
    assert "[login]" in result.sanitized_text
    assert "[secret]" in result.sanitized_text
    assert "[url]" in result.sanitized_text


def test_privacy_guard_does_not_redact_plain_claim_id() -> None:
    guard = get_default_privacy_guard()

    result = guard.sanitize_text("Что по заявке 6230173?")

    assert result is not None
    assert result.sanitized_text == "Что по заявке 6230173?"
    assert result.contains_sensitive_data is False


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

    assert result.contains_sensitive_data is True
    assert result.sanitized_value == {
        "text": "мой телефон [phone]",
        "items": [
            "email [email]",
            {"note": "пароль [password]"},
        ],
    }
