"""#166 Срез A — голосовой react-гейт Telegram (детерминированные контракты).

Покрывает машинно-проверяемые куски новой голосовой ветки
``telegram_inbound._process_approved_turn_locked`` (вынесены в чистые хелперы,
ПРАВИЛО #7): изоляцию тенанта в гейте и нормализацию расшифровки (.strip()).
Диспетчеризация (react vs «Не расслышала» vs стоп) — тонкая обвязка вокруг этих
предикатов, проверяется живым прогоном (как и текстовый react-гейт — отдельного
inbound-харнеса в проекте нет).
"""

from __future__ import annotations

from sreda.services.telegram_inbound import _clean_transcript, _voice_to_react_gate

_ENABLED = frozenset({"tenant_tg_755682022", "tenant_max_40921122"})


def test_gate_voice_flagged_not_new_true():
    assert _voice_to_react_gate(
        "voice", is_new_user=False, tenant_id="tenant_tg_755682022",
        enabled_tenants=_ENABLED,
    ) is True


def test_gate_non_flagged_tenant_false():
    """Изоляция: не-флагованный тенант голос НЕ уводит в react (нулевой регресс)."""
    assert _voice_to_react_gate(
        "voice", is_new_user=False, tenant_id="tenant_tg_999",
        enabled_tenants=_ENABLED,
    ) is False


def test_gate_text_message_false():
    """Текст блок не трогает (его ведёт обычный react-гейт ниже)."""
    assert _voice_to_react_gate(
        "text", is_new_user=False, tenant_id="tenant_tg_755682022",
        enabled_tenants=_ENABLED,
    ) is False


def test_gate_new_user_false():
    """Новичок (онбординг) — голос в react НЕ уводим by design."""
    assert _voice_to_react_gate(
        "voice", is_new_user=True, tenant_id="tenant_tg_755682022",
        enabled_tenants=_ENABLED,
    ) is False


def test_gate_empty_enabled_set_false():
    """Флаг пуст (дефолт) → никого не трогаем."""
    assert _voice_to_react_gate(
        "voice", is_new_user=False, tenant_id="tenant_tg_755682022",
        enabled_tenants=frozenset(),
    ) is False


def test_clean_transcript_strips():
    assert _clean_transcript({"text": "  купи молоко  "}) == "купи молоко"


def test_clean_transcript_whitespace_only_is_none():
    """Codex high R1: пробельная расшифровка НЕ должна уйти в react как текст."""
    assert _clean_transcript({"text": "   "}) is None


def test_clean_transcript_empty_and_missing_and_nondict():
    assert _clean_transcript({"text": ""}) is None
    assert _clean_transcript({"text": None}) is None
    assert _clean_transcript({}) is None
    assert _clean_transcript(None) is None
