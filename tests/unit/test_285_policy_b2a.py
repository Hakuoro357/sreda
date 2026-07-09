"""#285 Фаза B срез B2a: двухъярусная политика единого пути (чистая функция) — калибровка.

compute_unified_policy соединяет B1-сигналы с доменной онтологией #221. Проверяем ядро плана:
- ярус (а): командный сигнал + домен → allowed_write (прямой write); «поставь чайник» → ∅ (кандидат).
- контракт B1↔B2: «поставь чайник» (форма-команда, нет домена) НЕ даёт allowed_write.
- нейтрализация route-мины: «как дела?» (route→checklists, но w=False) → write ∅, read web-only.
- декларатив → memory-write; read-кюс → own-data read; baseline web всегда.
"""

from __future__ import annotations

import pytest

from sreda.runtime.react_policy import compute_unified_policy
from sreda.runtime.react_preflight import route_domains


def _pol(text):
    return compute_unified_policy(text, route_domains(text))


# ─────────── ярус (а): детерминированный write-грант (не-memory) ───────────
@pytest.mark.parametrize("text,write_dom", [
    ("добавь молоко", "shopping"),
    ("создай список покупок", "shopping"),
    ("удали задачу", "tasks"),
])
def test_tier_a_write_grant(text, write_dom):
    p = _pol(text)
    assert write_dom in p["allowed_write"], (text, p)
    assert p["signals"]["write_cmd"] is True


def test_declarative_grants_memory_write():
    """Прямой memory-write (ярус а) — ТОЛЬКО по декларативу («меня зовут»)."""
    p = _pol("меня зовут Таня")
    assert p["allowed_write"] == ["memory"]
    assert p["signals"]["declarative"] is True


@pytest.mark.parametrize("text", [
    "сохрани в тайне",       # идиома — route→memory, но НЕ прямой write (B2 субагент MAJOR)
    "сохрани в секрете",
    "запиши в дневник",
    "запомни, что я не ем глютен",  # командный memory-write → кандидат, не прямой
    "сохрани рецепт борща",
])
def test_memory_command_not_direct_write(text):
    """Командный/идиомный memory-write НЕ даёт прямой allowed_write (memory) — идёт через ярус (б)
    confirm (candidate). Прямой memory — только декларатив. Закрывает идиому «сохрани в тайне»."""
    p = _pol(text)
    assert "memory" not in p["allowed_write"], (text, p)
    assert p["confirm_write"] is True  # candidate/confirm активен


# ─────────── контракт B1↔B2: форма-команда без домена → НЕ грант ───────────
def test_imperative_without_domain_no_write_grant():
    """«поставь чайник» — форма-команда (B1 True), но домена нет → allowed_write ∅ → кандидат/confirm
    (B2b), НЕ молчаливая запись. Ядро контракта B1↔B2."""
    p = _pol("поставь чайник")
    assert p["signals"]["write_cmd"] is True   # форма распознана
    assert p["allowed_write"] == []            # но грант не выдан (нет домена)
    assert p["confirm_write"] is True          # write-попытка пойдёт через confirm


# ─────────── нейтрализация route-мины на смолтоке ───────────
def test_smalltalk_no_write_no_owndata():
    """«как дела?»: #221 route даёт checklists, но w=False → write ∅; read-кюс пуст → own-data закрыт;
    остаётся baseline web (анти-регресс роутера — ключевой red-тест плана)."""
    p = _pol("как дела?")
    assert p["allowed_write"] == []
    assert "checklists" not in p["allowed_read"]      # route-домен НЕ течёт в read
    assert p["allowed_read"] == ["web"]               # только baseline
    assert p["signals"]["read_cues"] == []


# ─────────── read-кюс → own-data (bounded) ───────────
def test_read_cue_opens_bounded_owndata():
    p = _pol("перескажи напоминания")
    assert p["allowed_write"] == []                    # read, не write
    assert "reminders" in p["allowed_read"]
    assert "web" in p["allowed_read"]
    # bounded: НЕ открывает чужие own-data домены
    assert "memory" not in p["allowed_read"] and "checklists" not in p["allowed_read"]


def test_baseline_web_always_present():
    for text in ("расскажи анекдот", "как дела?", "добавь молоко"):
        assert "web" in _pol(text)["allowed_read"], text


def test_write_domains_also_readable():
    """Что разрешено писать — разрешено и читать (найти объект правки): «удали задачу» → tasks в обоих."""
    p = _pol("удали задачу")
    assert "tasks" in p["allowed_write"] and "tasks" in p["allowed_read"]


# ─────────── confirm_write всегда активен на execute (ярус б жив) ───────────
def test_confirm_write_always_on():
    for text in ("добавь молоко", "как дела?", "поставь чайник", "перескажи напоминания"):
        assert _pol(text)["confirm_write"] is True, text


# ─────────── reported/цитата не даёт write (сквозь B1) ───────────
def test_reported_command_no_write():
    for text in ("добавь молоко, сказал он", 'команда "удали задачу" не работает'):
        p = _pol(text)
        assert p["allowed_write"] == [], (text, p)
        assert p["signals"]["write_cmd"] is False


# ─────────── #319 sticky-by-use: «дверь открыта, пока ею пользуются» ───────────
def _pol_sticky(text):
    return compute_unified_policy(text, route_domains(text), sticky_memory_write=True)


def test_sticky_opens_memory_for_bare_data():
    """Прошлый ход записал в память → голые данные серии («бёдра 865») пишутся без confirm."""
    p = _pol_sticky("бёдра 865")
    assert "memory" in p["allowed_write"], p
    assert p["signals"]["sticky_memory"] is True
    # без sticky тот же текст — кандидат (memory НЕ в ярусе а)
    p0 = _pol("бёдра 865")
    assert "memory" not in p0["allowed_write"], p0
    assert p0["signals"]["sticky_memory"] is False


def test_sticky_kept_on_explicit_memory_command():
    """«запиши спинки 900» мид-серии (route→memory) — дверь НЕ закрывается (не хуже голых данных)."""
    p = _pol_sticky("запиши спинки 900")
    assert "memory" in p["allowed_write"], p
    assert p["signals"]["sticky_memory"] is True


def test_sticky_precleared_on_other_domain_command():
    """PRE-CLEAR: явная команда в ДРУГОЙ раздел («добавь молоко в покупки») серию не продолжает —
    memory НЕ в ярусе (а) этого хода (shopping — да, по обычному яруcу а)."""
    p = _pol_sticky("добавь молоко в покупки")
    assert "memory" not in p["allowed_write"], p
    assert "shopping" in p["allowed_write"], p
    assert p["signals"]["sticky_memory"] is False


def test_sticky_default_off():
    """Без sticky-параметра поведение прежнее (byte-identical политика)."""
    p = _pol("бёдра 865")
    assert p["signals"]["sticky_memory"] is False


def test_sticky_precleared_on_domainless_command():
    """R2 (medium MAJOR): БЕЗ-доменная команда («поставь чайник») при открытой двери НЕ получает прямой
    memory-write — контракт «нет домена → кандидат» sticky не ломает."""
    p = _pol_sticky("поставь чайник")
    assert p["signals"]["write_cmd"] is True and "memory" not in p["allowed_write"], p
    assert p["signals"]["sticky_memory"] is False
