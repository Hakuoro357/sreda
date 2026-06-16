"""#153 Фаза 2 — compose отмены напоминания (reminder_cancelled, fixed-text).

Чеклист приёмки #153 п.3: удаление одного напоминания → ``reminder_cancelled``
(фиксированный текст «Готово, напоминание отменила.»), НЕ ``reminder_set_ok``
(которому нужны when_phrase/what, которых у отмены нет).

Тесты RED-first: до реализации шаблона/контракта они падают (нет шаблона в
реестре, нет образца, нет контракта).
"""

from __future__ import annotations


def test_reminder_cancelled_renders_fixed_text() -> None:
    """reminder_cancelled рендерится фиксированным текстом без переменных."""
    from sreda.services.composer import render

    out = render("reminder_cancelled", {})
    assert out == "Готово, напоминание отменила."


def test_reminder_cancelled_in_housewife_templates() -> None:
    """(a) Точка 1 регистрации — HOUSEWIFE_TEMPLATES (renderable + allowlist)."""
    from sreda.services.composer.templates_housewife import HOUSEWIFE_TEMPLATES

    assert "reminder_cancelled" in HOUSEWIFE_TEMPLATES


def test_reminder_cancelled_no_contract() -> None:
    """(b) Точка 2 регистрации — _COMPOSER_CONTRACTS = NO_CONTRACT
    (как generic_tool_error), НЕ пустой кортеж TEMPLATE_REQUIRED_KEYS."""
    from sreda.services.composer_contracts import (
        NO_CONTRACT,
        TEMPLATE_REQUIRED_KEYS,
        get_composer_contract,
    )

    assert get_composer_contract("reminder_cancelled") is NO_CONTRACT
    # фиксированный текст: НЕ заводим пустой/любой ключ в required-карте
    assert "reminder_cancelled" not in TEMPLATE_REQUIRED_KEYS


def test_reminder_cancelled_has_sample() -> None:
    """(c) Точка 3 регистрации — SAMPLE_TEMPLATE_DATA (анти-дрейф тесты)."""
    from sreda.services.composer_contracts import SAMPLE_TEMPLATE_DATA

    assert "reminder_cancelled" in SAMPLE_TEMPLATE_DATA
    assert SAMPLE_TEMPLATE_DATA["reminder_cancelled"] == {}


def test_reminder_cancelled_passes_composer_allowlist() -> None:
    """Шаблон проходит composer-allowlist валидатор — план, отмеряющий
    cancel_reminder с compose=reminder_cancelled, ВАЛИДЕН (template_id известен
    реестру и не runtime-only)."""
    from sreda.runtime.planner.schemas import CLARIFICATION_TEMPLATE_IDS
    from sreda.services.clarification_contract import RUNTIME_ONLY_TEMPLATE_IDS
    from sreda.services.composer import REGISTRY

    assert "reminder_cancelled" in REGISTRY.template_ids()
    # планировщик-эмитируемый: НЕ runtime-only, не в clarification-allowlist
    assert "reminder_cancelled" not in RUNTIME_ONLY_TEMPLATE_IDS
    assert "reminder_cancelled" not in CLARIFICATION_TEMPLATE_IDS


def test_cancel_reminder_compose_uses_reminder_cancelled_not_set_ok() -> None:
    """Чеклист #153 п.3: форма плана удаления одного напоминания собирается
    через reminder_cancelled (fixed-text), НЕ reminder_set_ok.

    Пин формы: cancel-успех (CancelReminderOk: только status='cancelled', НЕТ
    when_phrase/what) рендерится reminder_cancelled чисто, а reminder_set_ok на
    тех же (пустых) данных свалился бы в StrictUndefined."""
    import pytest
    from jinja2 import TemplateError

    from sreda.services.composer import render

    # reminder_cancelled — чистый рендер на пустых данных (нет полей у отмены)
    assert render("reminder_cancelled", {}) == "Готово, напоминание отменила."
    # reminder_set_ok на тех же данных — StrictUndefined (нужны when_phrase/what)
    with pytest.raises(TemplateError):
        render("reminder_set_ok", {})
