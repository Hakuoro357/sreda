"""#168 — фиксы react-пути: таймзона напоминаний (naive=МСК, не UTC) + читаемость
рецепта (снять markdown + разбить нумерованные шаги построчно)."""

from __future__ import annotations

from datetime import datetime, timezone

from sreda.runtime.react_loop import (
    _fmt, _format_lists, _parse_dt, _postformat, _strip_md,
)


# ---- Баг 2: таймзона ---------------------------------------------------------
def test_parse_dt_naive_is_msk_not_utc():
    """«11:00» без зоны трактуется как МСК → 08:00 UTC (раньше был баг: 11:00 UTC = 14:00 МСК)."""
    dt = _parse_dt("2026-06-19T11:00")
    assert dt.tzinfo == timezone.utc
    assert (dt.hour, dt.minute) == (8, 0), dt.isoformat()
    assert dt.day == 19


def test_parse_dt_aware_offset_respected():
    """Явный оффсет уважается (+03:00 → 08:00 UTC)."""
    assert _parse_dt("2026-06-19T11:00:00+03:00").hour == 8


def test_parse_dt_z_is_utc():
    """Z = UTC (11:00Z → 11:00 UTC), не сдвигаем."""
    assert _parse_dt("2026-06-19T11:00:00Z").hour == 11


# ---- Баг 1: markdown + шаги ---------------------------------------------------
def test_strip_md_removes_bold_and_headers():
    assert _strip_md("**Ингредиенты**") == "Ингредиенты"
    assert _strip_md("шаг __важный__ тут") == "шаг __важный__ тут"  # __ намеренно НЕ трогаем (R1)
    assert _strip_md("# Заголовок\nтекст") == "Заголовок\nтекст"


def test_format_lists_breaks_numbered_steps():
    out = _format_lists("Шаги: 1. Разогрейте масло. 2. Обжарьте мясо. 3. Добавьте лук.")
    assert "\n1. Разогрейте" in out, repr(out)
    assert "\n2. Обжарьте" in out
    assert "\n3. Добавьте" in out


def test_format_lists_keeps_decimals_and_units():
    """НЕ дробит «0.5 ч», «2 см.», «1 ч.», «≈ 20 минут» — нет вплотную «<цифра>. <Заглавная>»."""
    for s in ["0.5 ч. ложка соли", "покрывала на 2 см. Готовьте 20 минут",
              "1 ч. чайная ложка зиры", "готовьте ≈ 20 минут до мягкости"]:
        assert "\n" not in _format_lists(s), repr((s, _format_lists(s)))


def test_fmt_shows_msk_not_utc():
    """R1-фикс #168: _fmt показывает МСК (08:00 UTC → 11:00 МСК) — иначе Фредди эхом
    подтвердит смещённое время из tool-result."""
    assert "11:00" in _fmt(datetime(2026, 6, 19, 8, 0, tzinfo=timezone.utc))


def test_format_lists_single_numbered_not_broken():
    """R1-фикс #168: одиночное «N.» в прозе (порог ≥2 не набран) НЕ дробим."""
    assert "\n" not in _format_lists("Готовьте до 90. Снимите с огня и подавайте горячим.")
    assert "\n" not in _format_lists("Это версия 2. Работает стабильно.")


def test_format_lists_breaks_col0_sequence():
    """R2-фикс: список, начинающийся с «1.» в начале строки (без пробела слева), дробится."""
    out = _format_lists("1. Разогрейте масло. 2. Обжарьте мясо. 3. Добавьте лук.")
    assert out.startswith("1. Разогрейте"), repr(out)
    assert "\n2. Обжарьте" in out
    assert "\n3. Добавьте" in out


def test_format_lists_no_split_two_prose_ordinals():
    """R2-фикс: ДВЕ случайные порядковые в одной строке (90, 2 — не 1..N) НЕ дробим."""
    s = "Готовьте до 90. Снимите с огня. Это версия 2. Работает стабильно."
    assert "\n" not in _format_lists(s), repr(_format_lists(s))


def test_format_lists_no_double_newline_prebroken():
    """R2-фикс (по-строчная обработка): уже-разбитые шаги не получают двойной перенос."""
    assert _format_lists("1. Шаг раз\n2. Шаг два") == "1. Шаг раз\n2. Шаг два"


def test_format_lists_breaks_lowercase_sequence():
    """R2-фикс: строчные шаги-последовательность тоже дробятся (sequence-check страхует)."""
    out = _format_lists("1. разогрей масло 2. добавь лук")
    assert "\n2. добавь" in out, repr(out)


def test_format_lists_reminder_and_dashlist_preserved():
    """Регресс (Kimi R3): per-line рефактор не задел « — »-логику — напоминание
    «— title — time» цело (1 тире), а ≥3-пунктовый « — »-список дробится."""
    rem = "Вот напоминания:\n— разминка — 19 июня, 08:00"
    assert _format_lists(rem) == rem
    out = _format_lists("Список:\n— молоко — хлеб — яйца")
    assert out == "Список:\n— молоко\n— хлеб\n— яйца", repr(out)


def test_strip_md_preserves_math_and_paths():
    """R1-фикс #168: только ПАРНЫЙ жирный — «2**3»/«__init__» не мнём."""
    assert _strip_md("2**3 = 8") == "2**3 = 8"               # одиночный ** не парный
    assert _strip_md("__init__.py важен") == "__init__.py важен"  # __ не трогаем
    assert _strip_md("**Шаг** готов") == "Шаг готов"          # парный жирный снят


def test_postformat_recipe_readable():
    """Интеграция: markdown снят + нумерованные шаги построчно."""
    inp = ("**Ингредиенты** — рис — мясо **Приготовление** 1. **Обжарьте** мясо. "
           "2. **Добавьте** рис.")
    out = _postformat(inp)
    assert "**" not in out
    assert "\n1. Обжарьте" in out, repr(out)
    assert "\n2. Добавьте" in out
