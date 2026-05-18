"""R-39 Slice 6.1: тесты resolve_r39_mode."""

from __future__ import annotations

from typing import Any

import pytest

from sreda.agents.r39_mode import resolve_r39_mode


class _StubSession:
    """Подмена SQLAlchemy session — не используется внутри функции
    напрямую, только передаётся в runtime_config.get_config (которое
    мы патчим через monkeypatch).
    """
    pass


def _patch_runtime_config(monkeypatch, *, pilot: str = "", shadow: str = "",
                          fail_on: str | None = None) -> None:
    """Подмена sreda.services.runtime_config.get_config.

    fail_on: если задан, на этот ключ raises RuntimeError.
    """
    import sreda.services.runtime_config as rc_module

    def fake_get_config(session, key):
        if fail_on == key:
            raise RuntimeError(f"config fetch failed for {key}")
        if key == "r39_pilot_tenant_ids":
            return pilot
        if key == "r39_shadow_tenant_ids":
            return shadow
        return ""

    monkeypatch.setattr(rc_module, "get_config", fake_get_config)


# ─── feature_key gate ────────────────────────────────────────────────


def test_non_housewife_feature_returns_off() -> None:
    """R-39 только для housewife_assistant."""
    result = resolve_r39_mode(
        tenant_id="42", feature_key="eds_monitor", session=_StubSession(),
    )
    assert result == "off"


def test_none_feature_returns_off() -> None:
    result = resolve_r39_mode(
        tenant_id="42", feature_key=None, session=_StubSession(),
    )
    assert result == "off"


# ─── live allowlist ──────────────────────────────────────────────────


def test_tenant_in_pilot_returns_live(monkeypatch) -> None:
    _patch_runtime_config(monkeypatch, pilot="42,99")
    result = resolve_r39_mode(
        tenant_id="42", feature_key="housewife_assistant", session=_StubSession(),
    )
    assert result == "live"


def test_tenant_in_shadow_returns_shadow(monkeypatch) -> None:
    _patch_runtime_config(monkeypatch, shadow="42")
    result = resolve_r39_mode(
        tenant_id="42", feature_key="housewife_assistant", session=_StubSession(),
    )
    assert result == "shadow"


def test_tenant_in_both_pilot_wins(monkeypatch) -> None:
    """Если в обоих списках — live приоритетнее."""
    _patch_runtime_config(monkeypatch, pilot="42", shadow="42")
    result = resolve_r39_mode(
        tenant_id="42", feature_key="housewife_assistant", session=_StubSession(),
    )
    assert result == "live"


def test_tenant_in_neither_returns_off(monkeypatch) -> None:
    _patch_runtime_config(monkeypatch, pilot="99,100", shadow="200")
    result = resolve_r39_mode(
        tenant_id="42", feature_key="housewife_assistant", session=_StubSession(),
    )
    assert result == "off"


def test_empty_allowlists_returns_off(monkeypatch) -> None:
    """Чистая система — все тенанты идут в legacy."""
    _patch_runtime_config(monkeypatch, pilot="", shadow="")
    result = resolve_r39_mode(
        tenant_id="352612382", feature_key="housewife_assistant",
        session=_StubSession(),
    )
    assert result == "off"


# ─── tenant_id types ────────────────────────────────────────────────


def test_tenant_id_int_coerced_to_str(monkeypatch) -> None:
    """Если caller передал int (не должен по типам, но defensive)."""
    _patch_runtime_config(monkeypatch, pilot="42")
    result = resolve_r39_mode(
        tenant_id=42,  # type: ignore[arg-type]
        feature_key="housewife_assistant", session=_StubSession(),
    )
    assert result == "live"


def test_tenant_id_alphanumeric_works(monkeypatch) -> None:
    """tenant_id может быть UUID-like alphanumeric."""
    _patch_runtime_config(monkeypatch, pilot="user_abc123, 42")
    result = resolve_r39_mode(
        tenant_id="user_abc123",
        feature_key="housewife_assistant", session=_StubSession(),
    )
    assert result == "live"


# ─── Defensive: runtime_config errors → off ─────────────────────────


def test_runtime_config_pilot_read_error_returns_off(monkeypatch) -> None:
    _patch_runtime_config(monkeypatch, fail_on="r39_pilot_tenant_ids")
    result = resolve_r39_mode(
        tenant_id="42", feature_key="housewife_assistant",
        session=_StubSession(),
    )
    assert result == "off"


def test_runtime_config_shadow_read_error_returns_off(monkeypatch) -> None:
    """Если pilot returned пусто и shadow упал — все равно off."""
    _patch_runtime_config(monkeypatch, fail_on="r39_shadow_tenant_ids")
    result = resolve_r39_mode(
        tenant_id="42", feature_key="housewife_assistant",
        session=_StubSession(),
    )
    assert result == "off"


# ─── Wildcard "*" support ────────────────────────────────────────────


def test_wildcard_shadow_matches_any_tenant(monkeypatch) -> None:
    """Wildcard '*' в shadow → все housewife тенанты в shadow."""
    _patch_runtime_config(monkeypatch, shadow="*")
    for tid in ("42", "tenant_max_40921122", "tenant_tg_882189769", "random_string"):
        assert resolve_r39_mode(
            tenant_id=tid, feature_key="housewife_assistant",
            session=_StubSession(),
        ) == "shadow", f"failed for {tid}"


def test_wildcard_pilot_matches_any_tenant(monkeypatch) -> None:
    """Wildcard '*' в pilot → все housewife тенанты live."""
    _patch_runtime_config(monkeypatch, pilot="*")
    assert resolve_r39_mode(
        tenant_id="anyone", feature_key="housewife_assistant",
        session=_StubSession(),
    ) == "live"


def test_wildcard_does_not_bypass_feature_key_gate(monkeypatch) -> None:
    """'*' shadow не включает для non-housewife skill'ов."""
    _patch_runtime_config(monkeypatch, shadow="*")
    assert resolve_r39_mode(
        tenant_id="any", feature_key="eds_monitor",
        session=_StubSession(),
    ) == "off"


def test_wildcard_with_other_tokens_works(monkeypatch) -> None:
    """'*, tenant_xyz' (wildcard + explicit) → wildcard выигрывает."""
    _patch_runtime_config(monkeypatch, shadow="*, tenant_max_40921122")
    assert resolve_r39_mode(
        tenant_id="random", feature_key="housewife_assistant",
        session=_StubSession(),
    ) == "shadow"


# ─── Pilot tenant сценарий (Boris) ──────────────────────────────────


def test_pilot_tenant_boris_scenario(monkeypatch) -> None:
    """Главный pilot сценарий: один tenant в live allowlist'е, остальные off."""
    _patch_runtime_config(monkeypatch, pilot="352612382")
    # Boris (pilot) — live
    assert resolve_r39_mode(
        tenant_id="352612382", feature_key="housewife_assistant",
        session=_StubSession(),
    ) == "live"
    # Другие тенанты — off
    assert resolve_r39_mode(
        tenant_id="otherprod", feature_key="housewife_assistant",
        session=_StubSession(),
    ) == "off"
