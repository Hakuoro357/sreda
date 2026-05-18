"""R-39: тесты пост-фактум аудита."""

from __future__ import annotations

import pytest

from sreda.agents.contracts import ResultKind, ToolJournalEntry
from sreda.agents.journal import ToolJournal
from sreda.agents.post_response_audit import AuditResult, audit_response


# ─── Фабрики ──────────────────────────────────────────────────────────


def _journal_with(tool_names: list[str]) -> ToolJournal:
    j = ToolJournal()
    for i, name in enumerate(tool_names):
        j.append(ToolJournalEntry(
            tool_name=name,
            action_index=i,
            result_kind=ResultKind.SUCCESS,
            result_data={"title": "X"},
            idempotency_key=f"k-{i}",
        ))
    return j


def _detector_always_clean(text: str, tools: set[str]) -> bool:
    return False


def _detector_always_unbacked(text: str, tools: set[str]) -> bool:
    return True


# ─── Чистый случай ────────────────────────────────────────────────────


def test_clean_response_returns_not_unbacked() -> None:
    journal = _journal_with(["schedule_reminder"])
    result = audit_response(
        final_text="Поставила «X» на 14:00",
        journal=journal,
        detector=_detector_always_clean,
    )
    assert isinstance(result, AuditResult)
    assert result.is_unbacked is False
    assert result.correction_pending is None
    assert "schedule_reminder" in result.called_tools


def test_clean_response_does_not_call_admin_alert() -> None:
    alerts: list[str] = []
    audit_response(
        final_text="OK",
        journal=_journal_with(["schedule_reminder"]),
        detector=_detector_always_clean,
        admin_alert_fn=alerts.append,
    )
    assert alerts == []


# ─── Обнаружение unbacked claim ───────────────────────────────────────


def test_unbacked_claim_triggers_admin_alert() -> None:
    alerts: list[str] = []
    result = audit_response(
        final_text="Поставила всё как надо",
        journal=ToolJournal(),  # пусто — ничего не вызывалось
        detector=_detector_always_unbacked,
        admin_alert_fn=alerts.append,
        tenant_id=352612382,
        turn_id="t-001",
    )
    assert result.is_unbacked is True
    assert result.alert_sent is True
    assert len(alerts) == 1
    alert = alerts[0]
    assert "R-39 L4 audit" in alert
    assert "352612382" in alert
    assert "t-001" in alert


def test_unbacked_correction_pending_set() -> None:
    """При unbacked → есть correction_pending для injection в next turn."""
    result = audit_response(
        final_text="Сохранила рецепт",
        journal=ToolJournal(),
        detector=_detector_always_unbacked,
    )
    assert result.correction_pending is not None
    assert "Извини" in result.correction_pending
    assert "не сделала" in result.correction_pending


def test_admin_alert_excludes_user_text_for_152fz() -> None:
    """152-ФЗ: алерт НЕ содержит исходного user_text."""
    alerts: list[str] = []
    audit_response(
        final_text="Поставила «секретный рецепт молочного коктейля для Бориса» на 14:00",
        journal=ToolJournal(),
        detector=_detector_always_unbacked,
        admin_alert_fn=alerts.append,
        tenant_id=42,
    )
    # Preview финальной строки попадёт, но это ответ бота, не user_text.
    # Контракт audit'а: НЕ принимаем user_text вообще.
    # Проверка: assert что в нашем API нет поля user_text
    import inspect
    from sreda.agents.post_response_audit import audit_response as fn
    sig = inspect.signature(fn)
    assert "user_text" not in sig.parameters


# ─── Защита от падений ────────────────────────────────────────────────


def test_detector_exception_does_not_crash_audit() -> None:
    def bad_detector(text: str, tools: set[str]) -> bool:
        raise RuntimeError("detector imploded")

    result = audit_response(
        final_text="X",
        journal=ToolJournal(),
        detector=bad_detector,
    )
    # При ошибке детектора — считаем not unbacked (fail-open для UX,
    # потому что false-positive алерт без основания хуже чем
    # пропущенный confab — мы и так его не поймали бы).
    assert result.is_unbacked is False


def test_admin_alert_exception_doesnt_crash() -> None:
    def bad_alert(_: str) -> None:
        raise RuntimeError("alert system down")

    result = audit_response(
        final_text="X",
        journal=ToolJournal(),
        detector=_detector_always_unbacked,
        admin_alert_fn=bad_alert,
    )
    # Аудит сам не упал, но alert_sent=False
    assert result.is_unbacked is True
    assert result.alert_sent is False


def test_empty_journal_called_tools_is_empty_set() -> None:
    result = audit_response(
        final_text="X",
        journal=ToolJournal(),
        detector=_detector_always_clean,
    )
    assert result.called_tools == set()


def test_failed_entries_still_counted_as_called() -> None:
    """FAILURE-запись в журнале — это тоже попытка вызова tool'а."""
    journal = ToolJournal()
    journal.append(ToolJournalEntry(
        tool_name="schedule_reminder",
        action_index=0,
        result_kind=ResultKind.FAILURE,
        result_data={},
        error_message="db_error",
        idempotency_key="k1",
    ))
    result = audit_response(
        final_text="Поставила",
        journal=journal,
        detector=_detector_always_clean,
    )
    # Имя tool'а попадает в called_tools, потому что детектор смотрит
    # на «было вообще намерение вызвать или нет»
    assert "schedule_reminder" in result.called_tools


# ─── Кати-сценарий (главный регрессионный) ──────────────────────────


def test_kati_cleanly_audited_when_tools_match() -> None:
    """Реальный Кати-ход: replace_reminder вызвался, final_text — про
    замену; detector не должен сработать."""
    journal = _journal_with(["replace_reminder"])

    def realistic_detector(text: str, tools: set[str]) -> bool:
        # Упрощённая аналогия prod-детектора: если «поставила» в тексте
        # и schedule/replace в tools — OK.
        if "поставила" in text.lower() or "поменяла" in text.lower():
            return not bool(tools & {"schedule_reminder", "replace_reminder"})
        return False

    result = audit_response(
        final_text="Поправила — теперь «Разбудить Катю» на сегодня в 14:00",
        journal=journal,
        detector=realistic_detector,
    )
    assert result.is_unbacked is False
    assert result.correction_pending is None


def test_kati_caught_when_text_claims_but_no_tool_called() -> None:
    """Симулируем оригинальный баг: text заявляет «отменяю и ставлю»,
    но журнал пустой → L4 ловит."""
    def realistic_detector(text: str, tools: set[str]) -> bool:
        low = text.lower()
        if "отменяю" in low or "ставлю" in low:
            return not bool(tools & {"cancel_reminder", "schedule_reminder", "replace_reminder"})
        return False

    alerts: list[str] = []
    result = audit_response(
        final_text="Поняла! Отменяю и ставлю на 14:00 — «Разбудить Катю».",
        journal=ToolJournal(),  # ПУСТО — точно как было в проде
        detector=realistic_detector,
        admin_alert_fn=alerts.append,
        tenant_id=352612382,
        turn_id="t-2026-05-17T13:21",
    )
    assert result.is_unbacked is True
    assert result.alert_sent is True
    assert "L4 audit" in alerts[0]
    assert result.correction_pending is not None
