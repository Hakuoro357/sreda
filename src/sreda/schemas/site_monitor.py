from pydantic import BaseModel


class SiteMonitorChangeEvent(BaseModel):
    claim_id: str
    change_type: str
    has_new_response: bool = False
    requires_user_action: bool = False
