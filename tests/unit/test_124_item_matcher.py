"""#124 срез 2 — нормализатор + токен-граничный матчер (анти-фабрикация).

Codex (план R2/R3, оба MAJOR): substring-матч принимал выдуманный
короткий пункт внутри длинного слова («рис» в «ирис», «соль» в
«фасоль») и матч через границы реплик. Матчер сверяет пункт как
непрерывную последовательность токенов С ГРАНИЦАМИ в ОДНОЙ реплике.
"""
from __future__ import annotations

from sreda.runtime.planner.item_matcher import (
    item_grounded_in_sources,
    normalize_for_match,
)


def test_normalize_nfkc_nbsp_yo_casefold_punct() -> None:
    # NBSP → пробел, схлопывание, ё→е, casefold, краевая пунктуация
    assert normalize_for_match("  Хлеб  Бородинский. ") == \
        normalize_for_match("хлеб бородинский")
    assert normalize_for_match("Ёлка!") == normalize_for_match("елка")
    assert normalize_for_match("Молоко,,,") == "молоко"


def test_grounded_exact_and_token_sequence() -> None:
    sources = ["купить хлеб и молоко", "ещё нужны яйца"]
    assert item_grounded_in_sources("хлеб", sources)
    assert item_grounded_in_sources("Молоко", sources)
    assert item_grounded_in_sources("хлеб и молоко", sources)  # последоват.
    assert item_grounded_in_sources("яйца", sources)


def test_substring_inside_word_rejected() -> None:
    """Ядро анти-фабрикации: короткий выдуманный пункт внутри слова
    НЕ считается обоснованным."""
    assert not item_grounded_in_sources("рис", ["купи ирис и зефир"])
    assert not item_grounded_in_sources("соль", ["возьми фасоль"])
    assert not item_grounded_in_sources("кот", ["рукоятка для лопаты"])


def test_cross_utterance_match_rejected() -> None:
    """Последовательность через границы РАЗНЫХ реплик не обоснована."""
    # «молоко хлеб» нет ни в одной ОДНОЙ реплике
    assert not item_grounded_in_sources(
        "молоко хлеб", ["нужно молоко", "и ещё хлеб"])


def test_fabricated_item_not_grounded() -> None:
    assert not item_grounded_in_sources("сапоги", ["купи хлеб и молоко"])


def test_empty_item_not_grounded() -> None:
    assert not item_grounded_in_sources("", ["что угодно"])
    assert not item_grounded_in_sources("   ", ["что угодно"])
