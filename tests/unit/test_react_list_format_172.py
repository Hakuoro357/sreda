"""vex#172: завершающая фраза после списка — с новой строки, не приклеена к пункту.

Регрессионный target — Telegram tenant_max_40921122, 2026-06-18: список церквей
закончился строкой «— Хамовники Готово, рассказала.» (тёплая концовка приклеилась к
последнему пункту вместо новой строки). Правило добавлено в <style> ``_system_prompt``.

Presence-тест react-промпта по смысловым якорям (lowercase, устойчиво к регистру).
"""

from __future__ import annotations

from sreda.runtime.react_loop import _system_prompt


def _sp() -> str:
    return _system_prompt("2030-01-01 (Вторник)").lower()


def test_closing_phrase_after_list_on_new_line():
    sp = _sp()
    assert "после списка" in sp
    assert "с новой строки" in sp
    # явный запрет приклеивать к последнему пункту
    assert "не приклеивай к последнему" in sp


def test_gotovo_not_for_reference_answers_178():
    """#178: «Готово» — только подтверждение реального действия, не закрывашка справки/погоды."""
    sp = _sp()
    assert "погоду" in sp  # справку/поиск/погоду — отвечаем без слов-подтверждения
    assert "не заканчивай словом «готово»" in sp  # явный запрет «Готово» на справке
