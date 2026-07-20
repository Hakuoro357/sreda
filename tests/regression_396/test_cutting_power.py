"""Доказательство РЕЖУЩЕЙ СПОСОБНОСТИ на уровне фикстур (не только инвариантов).

1. #393 (ЗАКРЫТ): при выключенном kill-switch фикса (``SREDA_REACT_POST_TOOL_REPORT_
   ENABLED=0``) фикстура ОБЯЗАНА покраснеть — филлер доходит до юзера, реплика не
   называет результат. Так «зелёная фикстура» доказано не пустая: она краснеет, когда
   фикс убирают. Прод-код НЕ трогаем — только kill-switch через env.
2. #390 (ОТКРЫТ): xfail-фикстура краснеет именно на техвыдаче в сыром выводе инструмента
   (а не по случайной ошибке) — проверяем причину явно.
"""
from __future__ import annotations

import pytest

from tests.unit.conftest import seed_telegram_user

from . import harness
from .fixtures import F390_SHOW_LISTS, F393_ADD, F_AMBIG_CANCEL_ARCHIVE_RESUMED
from .harness import Fixture, ScriptedTurn, ai_text, ai_tool, check_fixture, run_fixture
from .invariants import inv_ambiguous_cancel_zero_mutations


@pytest.mark.asyncio
async def test_393_fixture_goes_red_when_fix_disabled(db_session, monkeypatch):
    """С выключенным гейтом #393 филлер доходит до юзера → харнесс краснеет (cutting power)."""
    # validation_alias фикса #393 — SREDA_REACT_POST_TOOL_REPORT (не ..._ENABLED)
    monkeypatch.setenv("SREDA_REACT_POST_TOOL_REPORT", "0")
    from sreda.config.settings import get_settings
    get_settings.cache_clear()
    assert get_settings().react_post_tool_report_enabled is False  # sanity: гейт реально выключен

    u = seed_telegram_user(db_session)
    db_session.commit()
    dialog = await run_fixture(F393_ADD, db_session, u)
    problems = check_fixture(dialog, F393_ADD, db_session, u)

    assert problems, "фикс #393 выключен, но харнесс не покраснел — фикстура пустая!"
    # реплика осталась филлером «приняла к сведению» — ловим по нашим ожиданиям
    joined = " | ".join(problems).lower()
    assert "приняла к сведению" in joined or "не называет результат" in joined or "дача" in joined, \
        problems
    # но мутация РЕАЛЬНО состоялась (баг именно про ОТЧЁТ, не про действие)
    assert dialog.turns[0].db_diff.get("checklist_items") == 3, dialog.turns[0].db_diff


@pytest.mark.asyncio
async def test_390_xfail_is_for_tech_leak_reason(db_session):
    """#390 краснеет ИМЕННО на техвыдаче сырого вывода инструмента (не по случайной ошибке)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    dialog = await run_fixture(F390_SHOW_LISTS, db_session, u)
    problems = check_fixture(dialog, F390_SHOW_LISTS, db_session, u)

    assert problems, "#390 должен краснеть на текущем main"
    assert any("техвыдач" in p.lower() for p in problems), problems
    # и убедимся, что источник — англ. счётчики pending/done/total в квитанции
    receipts_text = " ".join(r.content for t in dialog.turns for r in t.receipts).lower()
    assert "pending" in receipts_text and "total" in receipts_text, receipts_text


@pytest.mark.asyncio
async def test_362_defect_repro_is_caught_by_inv4a(db_session):
    """Режущая способность #362: скриптуем САМ дефект — успешная запись (add_task ok) +
    ложная отмена в реплике — и убеждаемся, что харнесс краснеет на инв.4a. Доказывает: если
    модель/детектор вернёт «Отменила» поверх состоявшейся записи, харнесс это поймает."""
    defect = Fixture(
        id="362_defect_repro", bug="#362",
        summary="дефект-репро: add_task ok + ложная отмена в реплике",
        turns=[ScriptedTurn(
            "Сахар утром 16 и 2",
            [ai_tool("add_task", {"title": "сахар утром 16, вечером 2"}),
             ai_text("Отменила, ничего не делаю.")])])
    u = seed_telegram_user(db_session)
    db_session.commit()
    dialog = await run_fixture(defect, db_session, u)
    problems = check_fixture(dialog, defect, db_session, u)
    assert dialog.turns[0].db_diff.get("tasks") == 1, "запись должна была состояться"
    assert any("mutated_not_cancelled" in p for p in problems), problems


@pytest.mark.asyncio
async def test_projection_catches_inplace_archive_at_unchanged_count(db_session):
    """Load-bearing тест ЛОГИЧЕСКОЙ проекции: ambiguous-cancel резюмит «да» → архивирует список.
    count НЕ меняется (archive не удаляет строку), но status active→archived → mutated=True ТОЛЬКО
    через проекцию → inv_ambiguous_cancel краснеет. Единственное НЕмаскированное место, где проекция
    режет (иначе она декоративна — Opus анти-вакуум)."""
    u = seed_telegram_user(db_session)
    db_session.commit()
    dialog = await run_fixture(F_AMBIG_CANCEL_ARCHIVE_RESUMED, db_session, u)
    t1 = dialog.turns[1]                       # резюмированный ход
    assert t1.db_diff == {}, f"ждали НЕИЗМЕННЫЙ count (archive не удаляет строку), факт {t1.db_diff}"
    assert t1.mutated is True, "проекция обязана поймать status flip active→archived"
    v = inv_ambiguous_cancel_zero_mutations(dialog, turns=[1])
    assert v and v[0].invariant == "ambiguous_cancel", v


@pytest.mark.asyncio
async def test_projection_cutting_power_regress_to_count_only_loses_violation(db_session, monkeypatch):
    """Доказательство режущей силы проекции (анти-#74): регрессируем proj() до count-only
    (status/title→None, id сохраняем) → нарушение inv_ambiguous_cancel ИСЧЕЗАЕТ. Т.е. проекция
    НЕ декоративна: выкинь status/title — и load-bearing фикстура выше перестаёт краснеть."""
    orig = harness.state_snapshot

    def _count_only(session, tenant_id, user_id):
        return {k: {i: (None, None) for i in v} for k, v in orig(session, tenant_id, user_id).items()}

    monkeypatch.setattr(harness, "state_snapshot", _count_only)
    u = seed_telegram_user(db_session)
    db_session.commit()
    dialog = await run_fixture(F_AMBIG_CANCEL_ARCHIVE_RESUMED, db_session, u)
    t1 = dialog.turns[1]
    assert t1.mutated is False, "count-only: status flip невидим → mutated=False (проекция выключена)"
    v = inv_ambiguous_cancel_zero_mutations(dialog, turns=[1])
    assert v == [], "count-only убил нарушение → проекция БЫЛА load-bearing (режущая сила доказана)"
