"""Прогон фикстур живых багов через реальный handle_turn с проверкой СОСТОЯНИЯ БД.

GREEN_FIXTURES — закрытые баги + синтетика инвариантов: ассерт «нарушений нет».
OPEN_BUG_FIXTURES — открытые баги: ``xfail(strict=True)`` — красные на текущем main
(документируют живой баг), станут XPASS (→ падение strict-xfail) при починке — сигнал
«фикс приехал, снять маркер и сделать жёстким ассертом».
"""
from __future__ import annotations

import pytest

from tests.unit.conftest import seed_telegram_user

from .fixtures import GREEN_FIXTURES, OPEN_BUG_FIXTURES
from .harness import check_fixture, run_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("fx", GREEN_FIXTURES, ids=lambda f: f.id)
async def test_green_fixture(fx, db_session):
    u = seed_telegram_user(db_session)
    db_session.commit()
    dialog = await run_fixture(fx, db_session, u)
    problems = check_fixture(dialog, fx, db_session, u)
    assert not problems, f"[{fx.id}] {fx.bug}: " + "; ".join(problems)


@pytest.mark.asyncio
@pytest.mark.parametrize("fx", OPEN_BUG_FIXTURES, ids=lambda f: f.id)
@pytest.mark.xfail(strict=True, reason="открытый баг — красный до фикса; XPASS = фикс приехал")
async def test_open_bug_fixture(fx, db_session):
    u = seed_telegram_user(db_session)
    db_session.commit()
    dialog = await run_fixture(fx, db_session, u)
    problems = check_fixture(dialog, fx, db_session, u)
    assert not problems, f"[{fx.id}] {fx.bug}: " + "; ".join(problems)
