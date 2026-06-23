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


def test_prunable_and_rerun_unsafe_independent_165():
    """Обрезка-безопасность ≠ rerun-безопасность (Codex high R2 MAJOR): множества НЕЗАВИСИМЫ и
    ПЕРЕСЕКАЮТСЯ ровно по metered_read (web). metered_read семья — И prunable (опускать ок), И
    rerun-guarded (перевызывать на recovery нельзя — спалит слот квоты)."""
    overlap = react_loop._PRUNABLE_FAMILIES & react_loop._UNKEYED_WRITE_FAMILIES
    expected_overlap = {f for f, p in react_loop._FAMILY_WRITE_POLICY.items() if p == "metered_read"}
    assert overlap == expected_overlap, (
        "пересечение prunable∩rerun-unsafe должно быть РОВНО metered_read-семьями "
        "(prune-safe но rerun-unsafe); ничего лишнего"
    )


def test_unkeyed_covers_rerun_unsafe_policy_165():
    """_UNKEYED_WRITE_FAMILIES (guard recovery-добора) == РОВНО семьи policy unkeyed|metered_read —
    т.е. все, чей повтор на recovery вреден (задвоит сущность ИЛИ спалит слот квоты)."""
    expected = {f for f, p in react_loop._FAMILY_WRITE_POLICY.items()
                if p in ("unkeyed", "metered_read")}
    assert set(react_loop._UNKEYED_WRITE_FAMILIES) == expected


def test_current_prunable_state_pin_165():
    """Регресс-пин текущего безопасного состояния: режем shopping (idempotent) + web (metered_read);
    guard recovery-добора — 5 unkeyed-семей + web (metered_read, rerun-unsafe). Семью сделают
    replay-safe → "idempotent" → пин обновить ОСОЗНАННО."""
    # #202: recipes оснащена ключами (save_recipe/batch пишут op_id+hash на ctx-пути) →
    # idempotent → переехала из rerun-unsafe в prunable. Пин обновлён ОСОЗНАННО.
    assert react_loop._PRUNABLE_FAMILIES == frozenset({"shopping", "web", "recipes"})
    assert react_loop._UNKEYED_WRITE_FAMILIES == frozenset(
        {"menu", "household", "checklists", "memory", "web"})
