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

from .fixtures import F390_SHOW_LISTS, F393_ADD
from .harness import check_fixture, run_fixture


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
