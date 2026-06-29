# -*- coding: utf-8 -*-
"""#264 — подтверждение удаления называет пункт («убрать «куриное филе»»), не «позиции».

`_confirm_phrase` для remove_shopping_items/delete_checklist_item возвращает callable, который
по id достаёт названия. Сбой резолва → фолбэк на статичную фразу (не валит confirm).
"""
from __future__ import annotations

import os

import pytest

# EncryptedString (ChecklistItem.title) требует ключ — как в test_143_checklist_by_id.py.
os.environ.setdefault(
    "SREDA_ENCRYPTION_KEY",
    "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
)


class _Item:
    def __init__(self, title):  # noqa: ANN001
        self.title = title


def test_confirm_phrase_shopping_shows_item_names():
    from sreda.runtime.react_loop import _confirm_phrase

    class _Q:
        def filter(self, *a, **k):  # noqa: ANN002, ANN003
            return self

        def all(self):
            return [_Item("куриное филе"), _Item("молоко")]

    class _Sess:
        def query(self, *a):  # noqa: ANN002
            return _Q()

    ph = _confirm_phrase("remove_shopping_items", _Sess(), "t1", "u1")
    assert callable(ph)
    out = ph({"item_ids": ["id1", "id2"]})
    assert "куриное филе" in out and "молоко" in out
    assert "из списка покупок" in out


def test_confirm_phrase_fallback_on_resolve_error():
    from sreda.runtime.react_loop import _CONFIRM_PHRASE, _confirm_phrase

    class _BadSess:
        def query(self, *a):  # noqa: ANN002
            raise RuntimeError("db down")

    ph = _confirm_phrase("remove_shopping_items", _BadSess(), "t", "u")
    assert ph({"item_ids": ["x"]}) == _CONFIRM_PHRASE["remove_shopping_items"]


def test_confirm_phrase_fallback_on_empty():
    # нет id / нет названий → статичная фраза (не «убрать  из списка покупок»)
    from sreda.runtime.react_loop import _CONFIRM_PHRASE, _confirm_phrase

    class _Q:
        def filter(self, *a, **k):  # noqa: ANN002, ANN003
            return self

        def all(self):
            return []

    class _Sess:
        def query(self, *a):  # noqa: ANN002
            return _Q()

    ph = _confirm_phrase("remove_shopping_items", _Sess(), "t", "u")
    assert ph({"item_ids": []}) == _CONFIRM_PHRASE["remove_shopping_items"]
    assert ph({"item_ids": ["x"]}) == _CONFIRM_PHRASE["remove_shopping_items"]  # резолв пуст


def test_confirm_phrase_static_for_clear_all():
    # «очистить меню» удаляет ВСЁ → статичная безличная фраза (не callable).
    from sreda.runtime.react_loop import _CONFIRM_PHRASE, _confirm_phrase

    out = _confirm_phrase("clear_menu", None, "t", "u")
    assert out == _CONFIRM_PHRASE["clear_menu"]
    assert isinstance(out, str)


@pytest.fixture
def _scope_session():
    # Реальная in-memory БД: тенант t1 с ДВУМЯ юзерами (u1, u1b) — для проверки, что
    # резолв чек-листа scoped по user_id (а не только по тенанту). Чек-лист принадлежит u1b.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from sreda.db.base import Base
    from sreda.db.models.checklists import Checklist, ChecklistItem
    from sreda.db.models.core import Tenant, User

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Tenant(id="t1", name="T1"))
    s.add(User(id="u1", tenant_id="t1", telegram_account_id="1"))
    s.add(User(id="u1b", tenant_id="t1", telegram_account_id="2"))
    s.add(Checklist(id="cl_b", tenant_id="t1", user_id="u1b", title="Список Б", status="active"))
    s.add(ChecklistItem(id="it_b", checklist_id="cl_b", position=0, title="секретный пункт", status="pending"))
    # архивный чек-лист того же владельца — для проверки status=active
    s.add(Checklist(id="cl_arch", tenant_id="t1", user_id="u1b", title="Архив", status="archived"))
    s.add(ChecklistItem(id="it_arch", checklist_id="cl_arch", position=0, title="архивный пункт", status="pending"))
    s.commit()
    yield s
    s.close()


def test_confirm_phrase_checklist_scoped_by_user_264(_scope_session):
    # MAJOR (Codex high + субагент): пункт u1b НЕ резолвится для u1 (тот же тенант, другой
    # юзер) → статичная фраза; для владельца u1b — резолвится с названием.
    from sreda.runtime.react_loop import _CONFIRM_PHRASE, _confirm_phrase

    ph_other = _confirm_phrase("delete_checklist_item", _scope_session, "t1", "u1")
    assert ph_other({"item_id": "it_b"}) == _CONFIRM_PHRASE["delete_checklist_item"]  # чужой → статичная

    ph_owner = _confirm_phrase("delete_checklist_item", _scope_session, "t1", "u1b")
    out = ph_owner({"item_id": "it_b"})
    assert "секретный пункт" in out and "чек-лист" in out  # владелец → название


def test_confirm_phrase_checklist_archived_falls_back_264(_scope_session):
    # status=active: пункт архивного списка (delete его отвергнет) → статичная фраза.
    from sreda.runtime.react_loop import _CONFIRM_PHRASE, _confirm_phrase

    ph = _confirm_phrase("delete_checklist_item", _scope_session, "t1", "u1b")
    assert ph({"item_id": "it_arch"}) == _CONFIRM_PHRASE["delete_checklist_item"]
