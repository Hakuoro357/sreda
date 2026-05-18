"""R-39: тесты helpers для канарейки по тенантам.

R-39 R4: tenant_id — str (не int), `Tenant.id: String(64)`.
"""

from __future__ import annotations

from sreda.agents.tenant_allowlist import (
    is_in_pilot,
    is_r39_enabled,
    parse_canary_percent,
    parse_pilot_tenants,
    tenant_in_canary,
)


# ─── parse_pilot_tenants ─────────────────────────────────────────────


def test_parse_empty_returns_empty() -> None:
    assert parse_pilot_tenants(None) == frozenset()
    assert parse_pilot_tenants("") == frozenset()


def test_parse_single_id() -> None:
    assert parse_pilot_tenants("352612382") == frozenset({"352612382"})


def test_parse_comma_separated() -> None:
    assert parse_pilot_tenants("352612382,42,99") == frozenset({"352612382", "42", "99"})


def test_parse_json_array_form() -> None:
    assert parse_pilot_tenants("[352612382, 42]") == frozenset({"352612382", "42"})


def test_parse_space_separated() -> None:
    assert parse_pilot_tenants("352612382 42 99") == frozenset({"352612382", "42", "99"})


def test_parse_accepts_non_numeric_tokens() -> None:
    """R-39 R4: tenant_id может быть любой строкой (числовой, UUID, alphanumeric)."""
    result = parse_pilot_tenants("352612382, abc, 99, xxx")
    assert result == frozenset({"352612382", "abc", "99", "xxx"})


def test_parse_skips_empty_tokens_only() -> None:
    """Пустые / whitespace-only токены пропускаются, остальное оставляем."""
    result = parse_pilot_tenants("352612382, , 42")
    assert result == frozenset({"352612382", "42"})


# ─── is_in_pilot ─────────────────────────────────────────────────────


def test_is_in_pilot_true() -> None:
    assert is_in_pilot("352612382", "352612382,42") is True


def test_is_in_pilot_false() -> None:
    assert is_in_pilot("99", "352612382,42") is False


def test_is_in_pilot_empty_allowlist_returns_false() -> None:
    assert is_in_pilot("352612382", None) is False
    assert is_in_pilot("352612382", "") is False


# ─── parse_canary_percent ────────────────────────────────────────────


def test_parse_percent_valid() -> None:
    assert parse_canary_percent("5") == 5
    assert parse_canary_percent("25") == 25
    assert parse_canary_percent("100") == 100
    assert parse_canary_percent("  5  ") == 5  # пробелы


def test_parse_percent_empty_or_none() -> None:
    assert parse_canary_percent(None) == 0
    assert parse_canary_percent("") == 0


def test_parse_percent_invalid_returns_zero() -> None:
    """Защитное поведение: typo → 0, не случайная 100%."""
    assert parse_canary_percent("abc") == 0
    assert parse_canary_percent("5.5") == 0


def test_parse_percent_clamps_range() -> None:
    assert parse_canary_percent("-5") == 0
    assert parse_canary_percent("200") == 100


# ─── tenant_in_canary ────────────────────────────────────────────────


def test_canary_zero_excludes_everyone() -> None:
    assert tenant_in_canary("352612382", 0) is False
    assert tenant_in_canary("1", 0) is False


def test_canary_100_includes_everyone() -> None:
    assert tenant_in_canary("352612382", 100) is True
    assert tenant_in_canary("1", 100) is True


def test_canary_stable_for_same_input() -> None:
    """Один и тот же tenant_id всегда в одной когорте."""
    for _ in range(5):
        assert tenant_in_canary("352612382", 50) == tenant_in_canary("352612382", 50)


def test_canary_monotonic_widening() -> None:
    """Если тенант в 5%, он точно в 25% (и в 50% и в 100%).

    Это важная инвариантность: при расширении канарейки 5→25 уже
    переключённые тенанты остаются переключёнными.
    """
    for tid in range(1, 1000):
        tid_s = str(tid)
        if tenant_in_canary(tid_s, 5):
            assert tenant_in_canary(tid_s, 25)
            assert tenant_in_canary(tid_s, 50)
            assert tenant_in_canary(tid_s, 100)
            return
    raise AssertionError("Никто не попал в 5% когорту из 999 тенантов")


def test_canary_distribution_roughly_proportional() -> None:
    """5% когорта на 10000 sequential tenant_id — близко к 500."""
    count = sum(1 for tid in range(1, 10001) if tenant_in_canary(str(tid), 5))
    # Допуск ±100 (1%) — sha256 даёт хорошо распределённый bucket
    assert 400 <= count <= 600, f"Распределение странное: {count}/10000"


# ─── is_r39_enabled ──────────────────────────────────────────────────


def test_pilot_allowlist_overrides_canary() -> None:
    """Если тенант в allowlist'е — включён, даже если canary=0."""
    assert is_r39_enabled(
        "352612382", pilot_allowlist="352612382", canary_percent="0"
    ) is True


def test_canary_works_without_allowlist() -> None:
    """При canary=100 — все включены, даже без allowlist'а."""
    assert is_r39_enabled(
        "42", pilot_allowlist=None, canary_percent="100"
    ) is True


def test_no_allowlist_no_canary_returns_false() -> None:
    """Дефолт — старый стек."""
    assert is_r39_enabled(
        "42", pilot_allowlist=None, canary_percent=None
    ) is False


def test_only_pilot_tenant() -> None:
    """Главный сценарий Day 5: только один pilot tenant включён."""
    pilot = "352612382"
    other = "12345"
    assert is_r39_enabled(pilot, pilot_allowlist=pilot) is True
    assert is_r39_enabled(other, pilot_allowlist=pilot) is False
