"""ReAct прозрачность Фредди — #178/#179/#180 (один промпт-проход 2026-06-19).

Симптомы (прод, тенант Бориса):
- #178: «Готово» прилипало к справочным ответам (погода/поиск).
- #179: спросил город, хотя есть в памяти; на «почему?» оправдался задним числом.
- #180: напоминание без времени — молча 09:00, не назвал момент в подтверждении.

Presence-тест промпта по смысловым якорям (lowercase). Качество поведения — review + живой прогон.
"""

from __future__ import annotations

from sreda.runtime.react_loop import _system_prompt


def _sp() -> str:
    return _system_prompt("2030-01-01 (Вторник)").lower()


def test_rule11_transparency_names_moment_and_default_180():
    sp = _sp()
    assert "прозрачность допущений" in sp
    assert "всегда называй" in sp          # итоговый момент напоминания
    assert "по умолчанию" in sp            # помечать дефолтное время


def test_rule12_memory_before_question_and_honest_179():
    sp = _sp()
    assert "сначала свои данные" in sp
    assert "загляни в память" in sp        # контекст из памяти ДО вопроса
    assert "не приписывай" in sp           # честность о своих действиях, без конфабуляции
