"""R1-фиксы аудита 2026-07-18, область W6 (misc MINORs).

- MINOR housewife_shopping: «бад» матчится ЦЕЛЫМ словом (не подстрокой в
  «бадьян»/«бадминтон»).
- MINOR monitor_health: send_telegram_alert возвращает bool (доставлено?).

(C9/adv-2/adv-3/M17-M19/trace/provider — покрыты своими сьютами:
test_audit_fix_secrets, test_204_phase3_cancel_legacy, test_capabilities_map,
test_provider_balances, test_audit_fix_ops_svc.)
"""

from __future__ import annotations


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
