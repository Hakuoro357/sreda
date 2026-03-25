from pydantic import BaseModel


class SiteMonitorDecision(BaseModel):
    should_send: bool
    reason_code: str | None = None
