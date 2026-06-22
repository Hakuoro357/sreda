"""#165 Фаза 5 (разблокировка #163): инвариант «нет durable-write-инструмента без key-policy в
ПРУНАБЕЛЬНОЙ семье».

``_FAMILY_WRITE_POLICY`` — единый источник истины классификации ленивых семей; ``_PRUNABLE_FAMILIES``
и ``_UNKEYED_WRITE_FAMILIES`` ВЫВОДЯТСЯ из неё (не дрейфуют). Тесты гейтят: нельзя добавить семью с
durable-write без ключа в прунабельные (иначе обрезка+повтор на recovery-проходе задвоили бы запись).
"""

from __future__ import annotations

from sreda.runtime import react_loop


def test_every_lazy_family_classified_165():
    """Каждая ленивая семья имеет запись write-policy — нет молчаливо неклассифицированной семьи."""
    assert set(react_loop._FAMILY_WRITE_POLICY) == set(react_loop._LAZY_FAMILIES), (
        "каждая ленивая семья ОБЯЗАНА быть в _FAMILY_WRITE_POLICY (keyed/readonly/unkeyed) — "
        "иначе семья с неизвестной политикой проскользнёт мимо инварианта"
    )


def test_policy_values_valid_165():
    """Допустимы только четыре значения политики."""
    assert set(react_loop._FAMILY_WRITE_POLICY.values()) <= {
        "idempotent", "readonly", "metered_read", "unkeyed"}


def test_prunable_only_safe_policies_165():
    """ГЛАВНЫЙ инвариант: прунабельная семья НИКОГДА не содержит durable-write ПОЛЬЗОВАТЕЛЬСКИХ
    данных, повтор которого задвоил бы сущность. Резать безопасно: idempotent (повтор=no-op любым
    механизмом), readonly (не пишет), metered_read (только best-effort счётчик квоты — двойной счёт терпим)."""
    for fam in react_loop._PRUNABLE_FAMILIES:
        assert react_loop._FAMILY_WRITE_POLICY[fam] in ("idempotent", "readonly", "metered_read"), (
            f"семья {fam!r} в _PRUNABLE, но её write-policy не idempotent/readonly/metered_read — "
            "обрезка+повтор на recovery-проходе задвоили бы запись сущности"
        )


def test_prunable_and_unkeyed_disjoint_165():
    """Прунабельные и unkeyed-write семьи не пересекаются (семья либо режется, либо карв-аут)."""
    assert react_loop._PRUNABLE_FAMILIES.isdisjoint(react_loop._UNKEYED_WRITE_FAMILIES)


def test_unkeyed_covers_all_unkeyed_policy_165():
    """Карв-аут (_UNKEYED_WRITE_FAMILIES) == РОВНО семьи с policy 'unkeyed' — ни одна не потеряна."""
    expected = {f for f, p in react_loop._FAMILY_WRITE_POLICY.items() if p == "unkeyed"}
    assert set(react_loop._UNKEYED_WRITE_FAMILIES) == expected


def test_current_prunable_state_pin_165():
    """Регресс-пин текущего безопасного состояния: режем shopping (keyed) + web (readonly); карв-аут —
    5 семей без ключа. Когда семью сделают replay-safe и переведут в "idempotent" — пин обновить ОСОЗНАННО."""
    assert react_loop._PRUNABLE_FAMILIES == frozenset({"shopping", "web"})
    assert react_loop._UNKEYED_WRITE_FAMILIES == frozenset(
        {"recipes", "menu", "household", "checklists", "memory"})
