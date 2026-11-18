from typing import Any

import httpx

from whatsapp_agent.config import get_settings


class BridgeClient:
    def __init__(self, base_url: str = "http://localhost:3001") -> None:
        self.base_url = base_url.rstrip("/")

    async def send_text(
        self,
        to: str,
        text: str,
        *,
        quoted_message_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"to": to, "text": text}
        if quoted_message_id:
            payload["quotedMessageId"] = quoted_message_id
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.base_url}/api/messages/send",
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def send_media(
        self,
        to: str,
        media_path: str,
        caption: str,
        *,
        quoted_message_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "to": to,
            "mediaPath": media_path,
            "caption": caption,
        }
        if quoted_message_id:
            payload["quotedMessageId"] = quoted_message_id
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/api/messages/send-media",
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def send_typing(self, to: str) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{self.base_url}/api/messages/typing",
                json={"to": to},
            )
