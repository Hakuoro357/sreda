"""#374: few-shot «покажи список X → показать пункты X» должен работать и при
ВЫКЛЮЧЕННОМ unified (legacy show_checklist/list_checklists), не только при ON.

Прод 2026-07-14: на «Что у меня в списке кино» модель ~50% показывает обзор всех
списков + покупки вместо пунктов «Кино». Причина — few-shot про конкретный список
был привязан к флагу SREDA_CHECKLIST_UNIFIED (OFF на проде): при OFF подсказки нет
и модель гадает. Разобрано: роль БД / RLS / гейт свежести #356 / смена модели —
исключены (A/B и прод-пробы). Корень — отсутствие legacy-подсказки.

RED до фикса: при unified OFF _system_prompt НЕ содержит примера про конкретный
список.
"""
from __future__ import annotations

import sreda.config.settings as sm
from sreda.runtime import react_loop


def _flag_unified(monkeypatch, on: bool) -> None:
    """SREDA_CHECKLIST_UNIFIED через env (канон #213/#197 тестов) + сброс кеша settings."""
    monkeypatch.setenv("SREDA_CHECKLIST_UNIFIED", "1" if on else "0")
    sm.get_settings.cache_clear()


def test_legacy_fewshot_present_when_unified_off(monkeypatch):
    """#374 приёмка: при unified OFF в системном промпте есть few-shot
    «покажи список X → show_checklist(X)» (пункты именно этого списка), и
    контрпример «какие списки → list_checklists» (обзор)."""
    _flag_unified(monkeypatch, False)
    sp = react_loop._system_prompt("")
    assert "покажи список кино" in sp, "нет few-shot про конкретный список при unified OFF"
    assert "show_checklist" in sp, "few-shot не ссылается на legacy show_checklist"
    assert "list_checklists" in sp, "нет контрпримера-обзора list_checklists"


def test_legacy_off_does_not_leak_get_checklist(monkeypatch):
    """При OFF примеры используют ТОЛЬКО legacy-инструменты — иначе модель
    зовёт get_checklist, которого в legacy-режиме нет."""
    _flag_unified(monkeypatch, False)
    sp = react_loop._system_prompt("")
    assert "get_checklist" not in sp, "при unified OFF промпт не должен упоминать get_checklist"


def test_unified_fewshot_unchanged_when_on(monkeypatch):
    """Регресс-защита: при unified ON остаётся get_checklist-версия примеров (#213)."""
    _flag_unified(monkeypatch, True)
    sp = react_loop._system_prompt("")
    assert 'get_checklist(mode="items"' in sp
    assert 'get_checklist(mode="overview"' in sp


def test_fewshot_never_empty(monkeypatch):
    """Подсказка про конкретный список есть в ОБОИХ режимах — не пусто ни при каком флаге."""
    for on in (True, False):
        _flag_unified(monkeypatch, on)
        sp = react_loop._system_prompt("")
        assert "покажи список кино" in sp, f"пустой few-shot при unified={'ON' if on else 'OFF'}"


def test_fewshot_in_stable_prefix_not_dynamic_tail(monkeypatch):
    """MINOR (sol/terra/субагент): few-shot ДОЛЖЕН стоять в стабильном префиксе — до
    динамического хвоста (`<context>` с датой/временем), иначе ломается prompt-кеш.
    Легаси-путь (непустой today) — хвост есть; проверяем позицию явно."""
    _flag_unified(monkeypatch, False)
    sp = react_loop._system_prompt("2026-07-14 (Monday)")
    assert "покажи список кино" in sp and "<context>" in sp
    assert sp.index("покажи список кино") < sp.index("<context>"), "few-shot попал в динамический хвост"


def test_off_fewshot_names_shopping(monkeypatch):
    """#374 приёмка (регресс покупок): OFF few-shot разводит покупки на list_shopping,
    чтобы «покажи список X → show_checklist» не потянул покупки в show_checklist."""
    _flag_unified(monkeypatch, False)
    sp = react_loop._system_prompt("")
    assert "list_shopping" in sp, "few-shot не разводит покупки"
    assert "покупок" in sp


def test_legacy_directive_distinguishes_named_from_overview(monkeypatch):
    """#374 корень (sol MAJOR): хвостовая checklist-директива при OFF РАЗЛИЧАЕТ конкретный
    список (show_checklist) от обзора (list_checklists) — раньше велела list_checklists всегда."""
    from sreda.runtime import react_preflight as rp
    _flag_unified(monkeypatch, False)
    hint = rp._section_hint("покажи список кино")
    assert hint is not None
    assert "show_checklist" in hint and "list_checklists" in hint
    assert "get_checklist" not in hint  # legacy НЕ упоминает unified-инструмент


def test_shopping_route_has_no_tail_directive(monkeypatch):
    """#374 R3: у «список покупок» НЕТ хвостовой shopping-директивы (None). R2 пробовал её дать,
    но она инжектилась и на write/compound/cross-ходы покупок и глушила их (sol/terra/субагент
    N1-N4) → откат. Покупки разводит few-shot + оговорка в _HINT_CHECKLIST."""
    from sreda.runtime import react_preflight as rp
    r = rp.route_domains("что у меня в списке покупок")
    assert r.primary_domain == "shopping"  # роутер уводит в shopping (не в чек-листы)
    assert r.directive is None             # без хвостовой директивы — не глушит write/compound


def test_legacy_directive_section_word_is_overview(monkeypatch):
    """#374 R3 (sol/субагент N5): в legacy checklist-директиве слово-раздел без имени
    («список дел», «дела») ЯВНО помечено как обзор — иначе модель приняла бы «дел» за имя списка
    и позвала show_checklist("дел") вместо верного обзора."""
    from sreda.runtime import react_preflight as rp
    _flag_unified(monkeypatch, False)
    hint = rp._section_hint("покажи список дел")
    assert hint is not None
    assert "список дел" in hint and "list_checklists" in hint  # раздел → обзор явно


def test_legacy_directive_mentions_shopping_separately(monkeypatch):
    """#374 R3 (sol collision на легаси/section-пути): checklist-директива оговаривает покупки
    как отдельный list_shopping — не даёт спутать «список покупок» с чек-листом, когда роутер
    disabled и «список покупок» уходит в section-ветку по слову «список»."""
    from sreda.runtime import react_preflight as rp
    _flag_unified(monkeypatch, False)
    hint = rp._section_hint("покажи список кино")
    assert "list_shopping" in hint  # покупки оговорены даже внутри checklist-директивы


def test_unified_on_examples_frozen(monkeypatch):
    """#374 R3 (terra/sol MINOR): ON-ветка few-shot заморожена golden — правка формулировки
    или перенос few-shot в хвост при ON покраснит тест (binding-констрейнт «ON байт-в-байт»)."""
    _flag_unified(monkeypatch, True)
    sp = react_loop._system_prompt("")
    ON_GOLDEN = (
        "Пользователь: «покажи список кино» → get_checklist(mode=\"items\", name=\"кино\") — "
        "пункты ИМЕННО названного списка; ответ строй из результата result_type=items.\n"
        "Пользователь: «какие у меня списки» → get_checklist(mode=\"overview\") — только "
        "названия со счётчиками, name НЕ передавай.\n"
    )
    assert ON_GOLDEN in sp, "ON few-shot изменился (нарушен binding-констрейнт byte-unchanged)"
