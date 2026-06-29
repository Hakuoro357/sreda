# -*- coding: utf-8 -*-
"""#264 — подтверждение удаления называет пункт («убрать «куриное филе»»), не «позиции».

`_confirm_phrase` для remove_shopping_items/delete_checklist_item возвращает callable, который
по id достаёт названия. Сбой резолва → фолбэк на статичную фразу (не валит confirm).
"""
from __future__ import annotations


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
