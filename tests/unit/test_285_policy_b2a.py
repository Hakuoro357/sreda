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
