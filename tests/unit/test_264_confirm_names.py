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
    from sreda.db.models.housewife import FamilyMember
    from sreda.db.models.housewife_food import Recipe
    from sreda.db.models.tasks import Task

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    s.add(Tenant(id="t1", name="T1"))
    s.add(User(id="u1", tenant_id="t1", telegram_account_id="1"))
    s.add(User(id="u1b", tenant_id="t1", telegram_account_id="2"))  # тот же тенант, другой юзер
    # Сущности «чужого» (u1b) пользователя в ТОМ ЖЕ тенанте — для проверки scope по user_id.
    s.add(Checklist(id="cl_b", tenant_id="t1", user_id="u1b", title="Список Б", status="active"))
    s.add(ChecklistItem(id="it_b", checklist_id="cl_b", position=0, title="секретный пункт", status="pending"))
    s.add(Checklist(id="cl_arch", tenant_id="t1", user_id="u1b", title="Архив", status="archived"))
    s.add(ChecklistItem(id="it_arch", checklist_id="cl_arch", position=0, title="архивный пункт", status="pending"))
    s.add(Recipe(id="rec_b", tenant_id="t1", user_id="u1b", title="Секретный борщ", source="user_dictated"))
    s.add(FamilyMember(id="fm_b", tenant_id="t1", user_id="u1b", name="Тайный дядя", role="self"))
    s.add(Task(id="task_b", tenant_id="t1", user_id="u1b", title="секретная задача"))
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


def test_confirm_phrase_recipe_scoped_by_user_264(_scope_session):
    from sreda.runtime.react_loop import _CONFIRM_PHRASE, _confirm_phrase

    ph_other = _confirm_phrase("delete_recipe", _scope_session, "t1", "u1")
    assert ph_other({"recipe_id": "rec_b"}) == _CONFIRM_PHRASE["delete_recipe"]  # чужой → статичная

    ph_owner = _confirm_phrase("delete_recipe", _scope_session, "t1", "u1b")
    out = ph_owner({"recipe_id": "rec_b"})
    assert "Секретный борщ" in out and "рецепт" in out  # владелец → название


def test_confirm_phrase_family_member_scoped_by_user_264(_scope_session):
    from sreda.runtime.react_loop import _CONFIRM_PHRASE, _confirm_phrase

    ph_other = _confirm_phrase("remove_family_member", _scope_session, "t1", "u1")
    assert ph_other({"member_id": "fm_b"}) == _CONFIRM_PHRASE["remove_family_member"]  # чужой → статичная

    ph_owner = _confirm_phrase("remove_family_member", _scope_session, "t1", "u1b")
    out = ph_owner({"member_id": "fm_b"})
    assert "Тайный дядя" in out and "член" in out  # владелец → имя


def test_confirm_phrase_move_task_scoped_by_user_264(_scope_session):
    from sreda.runtime.react_loop import _CONFIRM_PHRASE, _confirm_phrase

    ph_other = _confirm_phrase("move_task_to_checklist", _scope_session, "t1", "u1")
    assert ph_other({"task_id": "task_b"}) == _CONFIRM_PHRASE["move_task_to_checklist"]  # чужой → статичная

    ph_owner = _confirm_phrase("move_task_to_checklist", _scope_session, "t1", "u1b")
    out = ph_owner({"task_id": "task_b"})
    assert "секретная задача" in out and "перенести" in out  # владелец → название


def test_confirm_phrase_archive_checklist_scoped_by_user_264(_scope_session):
    # archive принимает id ИЛИ нечёткий фрагмент названия → резолв через find_list_by_title.
    from sreda.runtime.react_loop import _CONFIRM_PHRASE, _confirm_phrase

    ph_other = _confirm_phrase("archive_checklist", _scope_session, "t1", "u1")
    assert ph_other({"list_id_or_title": "Список"}) == _CONFIRM_PHRASE["archive_checklist"]  # чужой → статичная

    ph_owner = _confirm_phrase("archive_checklist", _scope_session, "t1", "u1b")
    out = ph_owner({"list_id_or_title": "Список"})  # фрагмент названия
    assert "Список Б" in out and "архивировать" in out  # владелец → название
