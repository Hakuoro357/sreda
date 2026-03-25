import httpx


class TelegramClient:
    def __init__(self, token: str) -> None:
        self.token = token

    async def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> dict:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.token}/answerCallbackQuery",
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def set_my_commands(self, commands: list[dict]) -> dict:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.token}/setMyCommands",
                json={"commands": commands},
            )
            response.raise_for_status()
            return response.json()

    async def send_media_group(
        self,
        chat_id: str,
        media: list[dict],
    ) -> dict:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.token}/sendMediaGroup",
                json={"chat_id": chat_id, "media": media},
            )
            response.raise_for_status()
            return response.json()

    async def send_photo(
        self,
        chat_id: str,
        photo_bytes: bytes,
        *,
        filename: str = "photo.jpg",
    ) -> dict:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{self.token}/sendPhoto",
                data={"chat_id": chat_id},
                files={"photo": (filename, photo_bytes, "image/jpeg")},
            )
            response.raise_for_status()
            return response.json()
