CORE_ASSISTANT = "core_assistant"
EDS_MONITOR = "eds_monitor"

# Feature keys that have been retired from the engine (#181). The skill's
# DB tables/migrations stay in place (quarantine until Phase 4) — this set
# is the single source of truth that the service layer consults to no-op
# every state-mutating entry point for a disabled skill. Adding a key here
# deactivates the skill engine-wide without touching any stored rows.
DISABLED_FEATURE_KEYS: frozenset[str] = frozenset({"eds_monitor"})


def is_feature_disabled(feature_key: str) -> bool:
    """True if ``feature_key`` is a retired skill (see DISABLED_FEATURE_KEYS)."""
    return feature_key in DISABLED_FEATURE_KEYS


def is_feature_enabled(feature_map: dict[str, bool], feature_key: str) -> bool:
    return bool(feature_map.get(feature_key, False))
