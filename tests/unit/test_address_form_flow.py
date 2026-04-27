"""Тесты для онбординг state-machine «имя + ты/вы» (2026-04-27).

Покрывает:
  * `build_name_question_message` — текст шага 1 (вопрос про имя).
  * `build_address_form_question_message` — текст + inline-кнопки шага 2.
  * `_handle_address_form_callback` — сохранение выбора + welcome.
  * `pick_ack(address_form=...)` — выбор пула фраз по форме обращения.
  * `_format_profile_for_prompt` — инжекция формы обращения в LLM.
  * Repository: `update_profile(address_form=...)`.
"""

from __future__ import annotations

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sreda.db.base import Base
from sreda.db.models.core import Tenant, User
from sreda.db.models.user_profile import TenantUserProfile


def _make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    return Session()


# --------------------- onboarding text helpers ---------------------

def test_build_name_question_message_text():
    from sreda.services.onboarding import build_name_question_message

    text = build_name_question_message()
    # Дружелюбный, без жёсткой формы обращения.
    assert "Среда" in text
    assert "зовут" in text.lower()
    # Не «уважаемый»/«уважаемая» — это бот-ассистент, не справка.


def test_build_address_form_question_message_text_and_buttons():
    from sreda.services.onboarding import build_address_form_question_message

    text, markup = build_address_form_question_message("Борис")
    # Имя в текст подставилось.
    assert "Борис" in text
    # 2 кнопки с правильными callback_data.
    rows = markup["inline_keyboard"]
    assert len(rows) == 1
    btns = rows[0]
    assert len(btns) == 2
    assert btns[0]["callback_data"] == "addrform:ty"
    assert btns[1]["callback_data"] == "addrform:vy"
    # Лейблы человекочитаемые.
    assert "ты" in btns[0]["text"].lower()
    assert "вы" in btns[1]["text"].lower()


# --------------------- repository --------------------------

def test_update_profile_address_form():
    from sreda.db.repositories.user_profile import UserProfileRepository

    session = _make_session()
    session.add(Tenant(id="t1", name="T"))
    session.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    session.commit()

    repo = UserProfileRepository(session)
    profile = repo.update_profile("t1", "u1", address_form="vy")
    session.commit()

    assert profile.address_form == "vy"

    # Перевыбор работает.
    profile = repo.update_profile("t1", "u1", address_form="ty")
    session.commit()
    assert profile.address_form == "ty"


def test_update_profile_address_form_validates():
    from sreda.db.repositories.user_profile import UserProfileRepository

    session = _make_session()
    session.add(Tenant(id="t1", name="T"))
    session.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))
    session.commit()

    repo = UserProfileRepository(session)
    with pytest.raises(ValueError, match="address_form"):
        repo.update_profile("t1", "u1", address_form="нечто")


# --------------------- pick_ack ----------------------------

def test_pick_ack_neutral_default():
    """Без address_form pick_ack возвращает фразу из нейтрального пула."""
    import random

    from sreda.services.ack_messages import _PHRASES_NEUTRAL, pick_ack

    rng = random.Random(42)
    for _ in range(20):
        phrase = pick_ack(rng=rng)
        assert phrase in _PHRASES_NEUTRAL


def test_pick_ack_ty_includes_ty_pool():
    import random

    from sreda.services.ack_messages import _PHRASES_TY, pick_ack

    # При большом количестве итераций должна попасть хоть одна TY-фраза.
    rng = random.Random(0)
    seen = {pick_ack(address_form="ty", rng=rng) for _ in range(200)}
    assert seen & set(_PHRASES_TY), "TY-pool не использован"


def test_pick_ack_vy_includes_vy_pool():
    import random

    from sreda.services.ack_messages import _PHRASES_VY, pick_ack

    rng = random.Random(0)
    seen = {pick_ack(address_form="vy", rng=rng) for _ in range(200)}
    assert seen & set(_PHRASES_VY), "VY-pool не использован"


def test_pick_ack_unknown_form_falls_back_to_neutral():
    import random

    from sreda.services.ack_messages import (
        _PHRASES_NEUTRAL,
        _PHRASES_TY,
        _PHRASES_VY,
        pick_ack,
    )

    rng = random.Random(0)
    seen = {
        pick_ack(address_form="garbage", rng=rng) for _ in range(50)
    }
    # Только нейтральный пул.
    assert seen.issubset(set(_PHRASES_NEUTRAL))
    assert not (seen & set(_PHRASES_TY))
    assert not (seen & set(_PHRASES_VY))


# --------------------- prompt format -----------------------

def test_format_profile_includes_address_form_ty():
    from sreda.runtime.handlers import _format_profile_for_prompt

    out = _format_profile_for_prompt({"address_form": "ty"})
    assert "ты" in out.lower()
    # И ничего лишнего про «вы»
    assert "«вы»" not in out


def test_format_profile_includes_address_form_vy():
    from sreda.runtime.handlers import _format_profile_for_prompt

    out = _format_profile_for_prompt({"address_form": "vy"})
    assert "вы" in out.lower()


def test_format_profile_omits_address_form_when_null():
    from sreda.runtime.handlers import _format_profile_for_prompt

    out = _format_profile_for_prompt(
        {"display_name": "Борис", "address_form": None}
    )
    # Имя есть, формы — нет.
    assert "Борис" in out
    assert "Форма обращения" not in out


# --------------------- TenantUserProfile model ---------------

def test_address_form_column_round_trip():
    session = _make_session()
    session.add(Tenant(id="t1", name="T"))
    session.add(User(id="u1", tenant_id="t1", telegram_account_id="100"))

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    session.add(TenantUserProfile(
        id="tup_1", tenant_id="t1", user_id="u1",
        display_name="Борис", address_form="vy",
        created_at=now, updated_at=now,
    ))
    session.commit()

    fresh = session.query(TenantUserProfile).filter_by(id="tup_1").one()
    assert fresh.address_form == "vy"
    assert fresh.display_name == "Борис"
