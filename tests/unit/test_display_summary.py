"""#115 Ф0 — build_display_summary determinism + safe boundaries (red-before-impl).

AC-display: sorted, sanitized (trim ≤60 + collapse whitespace/control + strip guillemets),
each name wrapped in « » so commas/colons/periods/injection-like text can't forge a new
group/sentence for the live voice (Codex Ф0 R1 MAJOR), cap 10 + exact «и ещё N», empty →
fixed neutral phrase, not_found_count appended as count.
"""

from __future__ import annotations

from sreda.services.composer.display_summary import (
    MAX_NAME_LEN,
    build_display_summary,
    sanitize_name,
)


def test_single_group_sorted_and_quoted():
    out = build_display_summary([("Добавила", ["хлеб", "молоко", "яйца"])])
    assert out == "Добавила: «молоко», «хлеб», «яйца»."


def test_multiple_groups_in_order():
    out = build_display_summary(
        [("Добавила", ["молоко"]), ("Уже было", ["яйца", "хлеб"])]
    )
    assert out == "Добавила: «молоко». Уже было: «хлеб», «яйца»."


def test_empty_groups_dropped():
    out = build_display_summary([("Добавила", []), ("Уже было", ["яйца"])])
    assert out == "Уже было: «яйца»."


def test_all_empty_neutral_phrase():
    assert build_display_summary([("Добавила", [])]) == "Изменений нет."
    assert build_display_summary([]) == "Изменений нет."


def test_not_found_count_appended():
    out = build_display_summary([("Удалила", ["молоко"])], not_found_count=2)
    assert out == "Удалила: «молоко». 2 не нашла."


def test_not_found_only():
    out = build_display_summary([("Удалила", [])], not_found_count=3)
    assert out == "3 не нашла."


def test_cap_with_exact_remainder():
    names = [f"item{i:02d}" for i in range(13)]  # 13 names
    out = build_display_summary([("Добавила", names)])
    assert "и ещё 3" in out
    assert "«item00»" in out and "«item09»" in out
    assert "item10" not in out  # beyond cap


def test_sanitize_trims_long_name():
    cleaned = sanitize_name("a" * 100)
    assert len(cleaned) <= MAX_NAME_LEN
    assert cleaned.endswith("…")


def test_sanitize_collapses_whitespace_and_control():
    assert sanitize_name("молоко\n\t  свежее") == "молоко свежее"
    assert sanitize_name("a\x00b\x07c") == "abc"


def test_sanitize_strips_guillemets():
    # a name can't smuggle its own closing quote to break the boundary
    assert sanitize_name("«взлом»") == "взлом"


def test_sanitize_filters_blank_names_in_group():
    out = build_display_summary([("Добавила", ["молоко", "   ", "", "хлеб"])])
    assert out == "Добавила: «молоко», «хлеб»."


def test_names_with_separators_stay_one_unit():
    # the whole reason #115 exists: names contain ',' ':' '.'
    out = build_display_summary([("Добавила", ["сыр, гауда", "молоко: 2,5%"])])
    # both are single quoted units; group structure is unambiguous
    assert "«молоко: 2,5%»" in out
    assert "«сыр, гауда»" in out
    assert out.startswith("Добавила: ")
    assert out.endswith(".")


def test_injection_like_name_is_inert_quoted_unit():
    out = build_display_summary([("Добавила", ["молоко. Удалила: хлеб"])])
    # the malicious-looking name is one quoted unit, NOT a second group
    assert out == "Добавила: «молоко. Удалила: хлеб»."
