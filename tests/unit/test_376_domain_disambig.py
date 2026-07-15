"""#376: subtract-only дизамбигуация неоднозначной read-кюс-группы.

На «Что у меня в списке кино» щедрый read_cue кладёт в allowed_read {checklists, shopping}
(слово «список» → группа), и mercury может позвать list_shopping (промах +покупки, прод
2026-07-15). Умный классификатор (disambiguator) стабильно говорит «checklists» → его
high-вердикт ВЫЧИТАЕТ остальных членов неоднозначной группы из allowed_read.

Инварианты (owner + ревью-трио R1/R2):
- ТОЛЬКО вычитание (add невозможен by construction): Фредди-домен вне поднятых → не применяем.
- write не трогаем (allowed_write исключён из subtract).
- compound/cross пропускаем (reminders/menu целы).
- disambiguator=None → байт-в-байт текущее поведение (#285/#352).

RED до реализации: параметр disambiguator в compute_unified_policy ещё не существует.
"""
from __future__ import annotations

from sreda.runtime.react_policy import compute_unified_policy
from sreda.runtime.react_preflight import DomainClassResult, route_domains


def _pol(text, disambiguator=None):
    return compute_unified_policy(text, route_domains(text), disambiguator=disambiguator)


def test_disambig_subtracts_shopping_376():
    """«список кино» + Фредди=checklists/high → shopping ВЫЧТЕН из allowed_read."""
    pol = _pol("Что у меня в списке кино", DomainClassResult(("checklists",), "high"))
    assert "checklists" in pol["allowed_read"]
    assert "shopping" not in pol["allowed_read"]  # член неоднозначной группы вычтен


def test_disambig_off_byte_identical_376():
    """disambiguator=None → shopping остаётся (текущее поведение не тронуто)."""
    pol = _pol("Что у меня в списке кино", None)
    assert "shopping" in pol["allowed_read"]


def test_disambig_low_untouched_376():
    """Фредди low → политику НЕ трогаем (fail-safe)."""
    pol = _pol("Что у меня в списке кино", DomainClassResult((), "low"))
    assert "shopping" in pol["allowed_read"]


def test_disambig_add_not_applied_376():
    """Фредди-домен ВНЕ поднятых (add-режим, возможная инъекция) → НЕ применяем.
    «покажи задачи» поднимает tasks; Фредди говорит shopping (не поднято) → allowed_read не трогаем."""
    text = "покажи задачи"
    pol = _pol(text, DomainClassResult(("shopping",), "high"))
    # shopping НЕ добавлен (add запрещён — только вычитание из поднятых)
    assert "shopping" not in pol["allowed_read"]


def test_disambig_compound_untouched_376():
    """Составной «кино и напомни хлеб» (compound) → дизамбигуация ПРОПУЩЕНА целиком:
    allowed_read с дизамбигуатором == без него (мой код не вмешивается в compound)."""
    text = "покажи список кино и напомни купить хлеб"
    r = route_domains(text)
    assert r.compound_by_connector, "тест-предпосылка: это compound"
    base = compute_unified_policy(text, r)
    dis = compute_unified_policy(text, r, disambiguator=DomainClassResult(("checklists",), "high"))
    assert base["allowed_read"] == dis["allowed_read"], "compound: дизамбигуация не должна менять read"
    assert dis["signals"]["disambig_kind"] is None, "compound: дизамбигуация должна быть пропущена"


def test_disambig_cross_untouched_376():
    """Cross «покупки из меню» → дизамбигуация пропущена, menu+shopping целы."""
    text = "составь покупки из меню"
    r = route_domains(text)
    pol = compute_unified_policy(text, r, disambiguator=DomainClassResult(("shopping",), "high"))
    if r.cross_intent:  # направленное кросс-намерение
        assert "menu" in pol["allowed_read"] and "shopping" in pol["allowed_read"]


def test_disambig_does_not_touch_write_376():
    """subtract НЕ снимает домен, который является write-целью (allowed_read ⊇ allowed_write инвариант)."""
    pol = _pol("Что у меня в списке кино", DomainClassResult(("checklists",), "high"))
    for wd in pol["allowed_write"]:
        assert wd in pol["allowed_read"], "write-цель осталась читаемой"
