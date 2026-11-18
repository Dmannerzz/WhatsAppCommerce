from typing import Any

from pydantic import BaseModel, Field


class MediaPayload(BaseModel):
    message_id: str
    from_jid: str
    chat_jid: str
    caption: str | None = None
    image_base64: str
    mime_type: str = "image/jpeg"
    quoted_message_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class CustomerMessagePayload(BaseModel):
    message_id: str
    from_jid: str
    chat_jid: str
    text: str = ""
    quoted_message_id: str | None = None
    sender_name: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class WholesalerMessagePayload(BaseModel):
    message_id: str
    from_jid: str
    chat_jid: str
    text: str = ""
    quoted_message_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
