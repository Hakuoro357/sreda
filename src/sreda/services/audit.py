def audit_event(action_type: str, details: dict | None = None) -> dict:
    return {"action_type": action_type, "details": details or {}}
