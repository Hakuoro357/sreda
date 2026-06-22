CORE_ASSISTANT = "core_assistant"

# Ретайренные скилы: движок/billing read-path/часть таблиц удалены, НО historical-строки в БД
# (subscription_plans eds_*, старые TenantSubscription/TenantFeature) остаются (FK/история).
# renew_cycle/next_payment_for_display/display ОПИРАЮТСЯ на этот хук, чтобы НЕ продлевать/не
# показывать ретайренную фичу (Codex R1 CRITICAL: пустой set → stale EDS-подписки вернулись бы
# в продление/показ). Также generic-гард для registry/loader.
RETIRED_FEATURE_KEYS = frozenset({"eds_monitor"})


def is_feature_disabled(feature_key: str) -> bool:
    """True if ``feature_key`` — ретайренный скил (#181: eds_monitor; движок удалён, но
    historical-строки в БД остаются). Используется renew_cycle/display + registry/loader."""
    return feature_key in RETIRED_FEATURE_KEYS


def is_feature_enabled(feature_map: dict[str, bool], feature_key: str) -> bool:
    return bool(feature_map.get(feature_key, False))
