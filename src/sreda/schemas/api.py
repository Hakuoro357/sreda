from pydantic import BaseModel


class TelegramWebhookAccepted(BaseModel):
    ok: bool
    request_id: str
