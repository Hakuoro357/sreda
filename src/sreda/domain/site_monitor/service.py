from dataclasses import dataclass


@dataclass(slots=True)
class SiteMonitorChangeSignal:
    claim_id: str
    change_type: str
    has_new_response: bool = False
    requires_user_action: bool = False
