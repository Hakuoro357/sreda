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


# ---------------------------------------------------------------------------
# Пин формы ПЛАНА через validate_plan (code-review R1+R2 [MINOR], оба Codex +
# субагент): рендер-пин выше изолирован — он не доказывает, что КАНОНИЧНЫЙ
# план одиночного удаления (list_reminders → cancel_reminder(.only) →
# compose=reminder_cancelled) проходит ВЕСЬ валидатор. Здесь — end-to-end через
# реальные специи и реальный validate_plan (без новой логики валидатора).
# ---------------------------------------------------------------------------


def _cancel_reminder_plan(compose):
    """Каноничная форма #153: list_reminders(title_match) с терминальной
    empty-веткой → cancel_reminder(reminder_id=${s1.items.only.reminder_id}) →
    переданный compose. Реальные специи REMINDERS."""
    from sreda.runtime.planner.schemas import (
        Action,
        OutcomeBranch,
        Plan,
        TurnClassification,
    )

    return Plan(
        turn_classification=TurnClassification(is_new_turn=True, reason="test"),
        actions={
            "s1": Action(
                tool="list_reminders",
                args={"title_match": "разминка"},
                expected_outcomes=[
                    OutcomeBranch(match={"status": "ok"}, next="s2"),
                    # .only требует терминальную empty-ветку продюсера
                    OutcomeBranch(match={"status": "empty"}),
                ],
                depends_on=[],
            ),
            "s2": Action(
                tool="cancel_reminder",
                # CancelReminderInput.reminder_id ← ровно-одно через .only
                args={"reminder_id": "${s1.items.only.reminder_id}"},
                # CancelReminderOk.status == 'cancelled' (НЕ 'ok')
                expected_outcomes=[OutcomeBranch(match={"status": "cancelled"})],
                depends_on=["s1"],
            ),
        },
        compose=compose,
    )


def _reminders_registry() -> dict:
    from sreda.services.tool_schemas.specs_reminders import (
        CANCEL_REMINDER_SPEC,
        LIST_REMINDERS_SPEC,
    )

    return {
        "list_reminders": LIST_REMINDERS_SPEC,
        "cancel_reminder": CANCEL_REMINDER_SPEC,
    }


def test_cancel_reminder_plan_with_reminder_cancelled_validates() -> None:
    """Чеклист #153 п.3 (плановый уровень): каноничный план удаления одного
    напоминания с ``compose=reminder_cancelled`` проходит ``validate_plan``
    без единого нарушения (template известен, contract=NO_CONTRACT, .only-ссылка
    ``${s1.items.only.reminder_id}`` валидна против ListRemindersItem)."""
    from sreda.runtime.planner.schemas import ComposerCall
    from sreda.runtime.planner.validator import validate_plan

    plan = _cancel_reminder_plan(
        ComposerCall(kind="template", template_id="reminder_cancelled", template_data={})
    )
    violations = validate_plan(plan, _reminders_registry())
    assert violations == [], (
        "каноничный план отмены (compose=reminder_cancelled) обязан быть валиден; "
        f"получено: {[(v.code, v.message) for v in violations]}"
    )


def test_cancel_reminder_plan_with_reminder_set_ok_is_rejected() -> None:
    """Чеклист #153 п.3 (негатив): тот же план отмены, но ОШИБОЧНО собранный
    через ``reminder_set_ok`` (шаблон создания, требует when_phrase/what,
    которых у отмены нет), отвергается валидатором по contract-нарушению — а не
    тихо проходит, чтобы потом упасть на рендере в проде."""
    from sreda.runtime.planner.schemas import ComposerCall
    from sreda.runtime.planner.validator import validate_plan

    plan = _cancel_reminder_plan(
        ComposerCall(kind="template", template_id="reminder_set_ok", template_data={})
    )
    violations = validate_plan(plan, _reminders_registry())
    contract_v = [v for v in violations if v.code == "composer_contract_invalid"]
    assert contract_v, (
        "compose=reminder_set_ok для отмены обязан давать composer_contract_invalid "
        f"(нет when_phrase/what); получено: {[(v.code, v.message) for v in violations]}"
    )
    msgs = " ".join(v.message or "" for v in contract_v)
    assert "when_phrase" in msgs and "what" in msgs, msgs
