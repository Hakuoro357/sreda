CORE_ASSISTANT = "core_assistant"
EDS_MONITOR = "eds_monitor"


def is_feature_enabled(feature_map: dict[str, bool], feature_key: str) -> bool:
    return bool(feature_map.get(feature_key, False))
