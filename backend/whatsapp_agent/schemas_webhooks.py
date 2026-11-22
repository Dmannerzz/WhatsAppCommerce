from typing import Any

from pydantic import AnyHttpUrl, BaseModel, Field, model_validator


class MediaPayload(BaseModel):
    message_id: str
    from_jid: str
    chat_jid: str
    caption: str | None = None
    image_base64: str | None = None
    image_url: AnyHttpUrl | None = None
    mime_type: str = "image/jpeg"
    quoted_message_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_one_image_source(self) -> "MediaPayload":
        if (self.image_base64 is None) == (self.image_url is None):
            raise ValueError("Provide exactly one of image_base64 or image_url")
        return self


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
