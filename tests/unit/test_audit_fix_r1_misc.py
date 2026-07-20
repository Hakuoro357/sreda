"""R1-фиксы аудита 2026-07-18, область W6 (misc MINORs).

- MINOR housewife_shopping: «бад» матчится ЦЕЛЫМ словом (не подстрокой в
  «бадьян»/«бадминтон»).
- MINOR monitor_health: send_telegram_alert возвращает bool (доставлено?).

(C9/adv-2/adv-3/M17-M19/trace/provider — покрыты своими сьютами:
test_audit_fix_secrets, test_204_phase3_cancel_legacy, test_capabilities_map,
test_provider_balances, test_audit_fix_ops_svc.)
"""

from __future__ import annotations

import pytest


def test_minor_bad_matches_whole_word_only() -> None:
    from sreda.services.housewife_shopping import _guess_category

    # «бадьян» (специя) больше НЕ классифицируется в «лекарства» через
    # подстроку «бад».
    assert _guess_category("бадьян") != "лекарства"
    assert _guess_category("бадминтон ракетка") != "лекарства"
    # «БАД» целым словом всё ещё → «лекарства».
    assert _guess_category("бад для суставов") == "лекарства"
    assert _guess_category("БАД омега-3") == "лекарства"
    # Другие (префиксные) keywords не сломаны.
    assert _guess_category("витамин д") == "лекарства"
    assert _guess_category("таблетки от головы") == "лекарства"


def test_minor_send_telegram_alert_returns_bool_without_chat_id(monkeypatch) -> None:
    import scripts.monitor_health as mh

    # Без chat_id доставка невозможна → False (caller не штампует alert-state).
    monkeypatch.setattr(mh, "ADMIN_CHAT_ID", "")
    monkeypatch.setattr(mh, "_ENV", {"SREDA_TELEGRAM_BOT_TOKEN": "tok"})
    assert mh.send_telegram_alert("x") is False

    # Без токена — тоже False.
    monkeypatch.setattr(mh, "ADMIN_CHAT_ID", "123")
    monkeypatch.setattr(mh, "_ENV", {})
    assert mh.send_telegram_alert("x") is False


# ---------------------------------------------------------------------------
# M13 (R1): non-ReAct путь прокидывает исход process_job (не глотает)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m13_handle_command_returns_process_job_outcome(monkeypatch):
    """_handle_command ВОЗВРАЩАЕТ исход process_job (раньше глотал → None →
    inbound метился 'processed' даже при failed)."""
    from unittest.mock import AsyncMock, MagicMock

    from sreda.services import telegram_bot as tb

    monkeypatch.setattr(tb, "dispatch_telegram_action", lambda **k: MagicMock())
    fake_runtime = MagicMock()
    fake_runtime.enqueue_action.return_value = MagicMock(job_id="job_1")
    fake_runtime.process_job = AsyncMock(return_value="failed")
    monkeypatch.setattr(tb, "ActionRuntimeService", lambda *a, **k: fake_runtime)

    out = await tb._handle_command(
        MagicMock(), telegram_client=MagicMock(), bot_key="sreda",
        payload={}, onboarding=MagicMock(), inbound_message_id="m1",
    )
    assert out == "failed"

    fake_runtime.process_job = AsyncMock(return_value="completed")
    out2 = await tb._handle_command(
        MagicMock(), telegram_client=MagicMock(), bot_key="sreda",
        payload={}, onboarding=MagicMock(), inbound_message_id="m1",
    )
    assert out2 == "completed"


@pytest.mark.asyncio
async def test_m13_interaction_propagates_outcome_for_text(monkeypatch):
    """handle_telegram_interaction для текст-пути прокидывает исход
    _handle_command наверх (для gate _turn_ok в telegram_inbound)."""
    from unittest.mock import AsyncMock, MagicMock

    from sreda.services import telegram_bot as tb

    # Плоский текст (не callback / не persona / не pb_tour: tenant/user None).
    monkeypatch.setattr(
        tb, "_maybe_transcribe_voice", AsyncMock(side_effect=lambda p, **k: p))
    monkeypatch.setattr(tb, "_extract_message_text", lambda p: "привет")
    # is_persona_settings_request импортится локально из housewife_persona.
    monkeypatch.setattr(
        "sreda.services.housewife_persona.is_persona_settings_request",
        lambda t: False,
    )
    monkeypatch.setattr(tb, "_handle_command", AsyncMock(return_value="failed"))

    onboarding = MagicMock()
    onboarding.chat_id = "1"
    onboarding.tenant_id = None  # → пропускает pb_tour-блок
    onboarding.user_id = None

    result = await tb.handle_telegram_interaction(
        MagicMock(), bot_key="sreda",
        payload={"message": {"chat": {"id": 1}, "text": "привет"}},
        telegram_client=MagicMock(), onboarding=onboarding,
        inbound_message_id="m1",
    )
    assert result == "failed"  # прокинулось (RED без фикса: было None)
