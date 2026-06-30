# -*- coding: utf-8 -*-
"""#267 A0 — безопасность confirm: свободный текст «удали Y» на confirm НЕ исполняет удаление.

CRITICAL (ревью R1): `_YES` содержал «удали»/«удаляй», `_is_yes` матчил префикс → текст
«удали напоминание не задачу» на confirm задачи читался как «да» → удалял НЕ ТО (инверсия
намерения). A0: классификация в handle_turn (classify_confirm_reply); в граф только канон «да»/«нет».
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from sreda.db.models.housewife import FamilyReminder
from sreda.runtime.react_loop import classify_confirm_reply, handle_turn
from tests.unit.conftest import seed_telegram_user
from tests.unit.test_react_loop_confirm_and_guards import _cancel_script, _seed_reminder


def test_classify_confirm_reply_267_a0():
    # affirm — строгий аффирматив, БЕЗ командных глаголов
    assert classify_confirm_reply("да") == "affirm"
    assert classify_confirm_reply("да!") == "affirm"  # пунктуация снимается
    assert classify_confirm_reply("подтверждаю") == "affirm"
    # negate
    assert classify_confirm_reply("нет") == "negate"
    assert classify_confirm_reply("отмена") == "negate"
    # CRITICAL: командный глагол / новое намерение → redirect, НЕ affirm
    assert classify_confirm_reply("удали напоминание не задачу") == "redirect"
    assert classify_confirm_reply("удали") == "redirect"
    assert classify_confirm_reply("хочу удалить напоминание") == "redirect"
    assert classify_confirm_reply("") == "redirect"


@pytest.mark.asyncio
async def test_a0_freetext_command_does_not_execute_delete_267(db_session):
    # CRITICAL red-тест: на confirm удаления текст «удали <другое>» (командный глагол) НЕ исполняет
    # удаление. БЕЗ A0 «удали омлет».startswith("удали ") → _is_yes=True → напоминание отменялось бы.
    u = seed_telegram_user(db_session)
    db_session.commit()
    rid = _seed_reminder(db_session, u)
    thread = f"react:t:{uuid4().hex}"

    r1 = await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread, llm=_cancel_script(rid),
        user_text="удали разминку", inbound_message_id="m1", channel="max")
    assert "удалю" in r1.lower(), r1  # confirm-пауза «Я сейчас удалю напоминание …»
    db_session.expire_all()
    assert db_session.get(FamilyReminder, rid).status == "pending", "до подтверждения не удалять"

    # ход2: свободный текст с КОМАНДНЫМ глаголом — НЕ «да»
    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread, llm=_cancel_script(rid),
        user_text="удали омлет из 3 яиц", inbound_message_id="m2", channel="max")
    db_session.expire_all()
    # A0: «удали омлет» → redirect → resume «нет» → напоминание ЦЕЛО (без A0 было бы cancelled)
    assert db_session.get(FamilyReminder, rid).status == "pending", \
        "A0: командный текст на confirm НЕ должен исполнять удаление"


@pytest.mark.asyncio
async def test_a0_plain_da_still_confirms_267(db_session):
    # Регресс: чистое «да» текстом по-прежнему подтверждает (affirm → канон «да»).
    u = seed_telegram_user(db_session)
    db_session.commit()
    rid = _seed_reminder(db_session, u)
    thread = f"react:t:{uuid4().hex}"
    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread, llm=_cancel_script(rid),
        user_text="удали разминку", inbound_message_id="m1", channel="max")
    await handle_turn(
        session=db_session, tenant_id=u.tenant_id, user_id=u.user_id,
        thread_id=thread, llm=_cancel_script(rid),
        user_text="да", inbound_message_id="m2", channel="max")
    db_session.expire_all()
    assert db_session.get(FamilyReminder, rid).status == "cancelled", "«да» текстом → подтверждение"
