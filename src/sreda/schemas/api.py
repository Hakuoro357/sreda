from pydantic import BaseModel


class TelegramWebhookAccepted(BaseModel):
    ok: bool
    # ``None`` when the update short-circuited without persisting an inbound row
    # (admin-login branch #305, soft-delete silent-drop, SignupBlocked): the
    # handler returns None → webhook must still respond 200/202, not 500.
    request_id: str | None = None
