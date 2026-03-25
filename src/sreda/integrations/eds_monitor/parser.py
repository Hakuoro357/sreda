def build_claim_change_signal(previous_hash: str | None, current_hash: str, status: str | None) -> dict:
    return {
        "previous_fingerprint_hash": previous_hash,
        "current_fingerprint_hash": current_hash,
        "status": status,
    }
