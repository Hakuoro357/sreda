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


def test_zero_width_split_rejected() -> None:
    """Codex R1 (оба) MAJOR: невидимый zero-width внутри слова не должен
    рождать токен-границу — выдуманный «соль» НЕ обоснован в «фа​соль»."""
    assert not item_grounded_in_sources("соль", ["возьми фа​соль"])
    assert not item_grounded_in_sources("рис", ["купи и​рис"])
    # и не ложно-отрицательный: «молоко» с невидимым внутри = «молоко»
    assert item_grounded_in_sources("молоко", ["нужно мо​локо"])


def test_hyphen_internal_not_boundary() -> None:
    """Codex R1: внутрисловный дефис не граница — «песок» НЕ обоснован
    в «сахар-песок», но «сахар-песок» обоснован целиком."""
    assert not item_grounded_in_sources("песок", ["купи сахар-песок"])
    assert not item_grounded_in_sources("то", ["сделай что-то"])
    assert item_grounded_in_sources("сахар-песок", ["купи сахар-песок"])


def test_decomposed_yo_and_iy_normalize() -> None:
    """Codex R1 MINOR: разложенные ё (е+◌̈) и й (и+◌̆) сводятся NFKC."""
    assert normalize_for_match("ёлка") == normalize_for_match("ёлка")
    assert normalize_for_match("йогурт") == normalize_for_match("йогурт")
    assert item_grounded_in_sources("ёлка", ["поставь ёлку"]) is False
    # (форма слова разная — это ОК; точное слово грунтуется:)
    assert item_grounded_in_sources("ёлка", ["поставь ёлка"])


def test_casefold_introduced_combining_mark_stripped() -> None:
    """Codex R2 high: casefold САМ вводит Mn (İ → i+◌̇); снятие Cf/Mn
    ПОСЛЕ casefold не даёт выдуманному «tem» обосноваться в «İtem»."""
    assert not item_grounded_in_sources("tem", ["İtem"])
    assert item_grounded_in_sources("item", ["İtem"])  # целое слово грунтуется
