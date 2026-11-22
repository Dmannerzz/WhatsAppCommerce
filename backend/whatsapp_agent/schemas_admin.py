from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from whatsapp_agent.models import OrderInquiryStatus


class BusinessSettingsUpdate(BaseModel):
    wholesale_group_jid: str | None = None
    wholesale_group_jids: list[str] | None = None
    retail_group_jid: str | None = None
    mum_admin_jid: str | None = None
    wholesaler_jids: list[str] = Field(default_factory=list)
    markup_rule: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    min_profit_rule: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=12,
        decimal_places=2,
    )
    payment_instructions: str = Field(default="", max_length=2000)
    automation_enabled: bool = True

    @field_validator(
        "wholesale_group_jid",
        "retail_group_jid",
        "mum_admin_jid",
        mode="before",
    )
    @classmethod
    def blank_jids_are_none(cls, value: str | None) -> str | None:
        if isinstance(value, str) and not value.strip():
            return None
        return value.strip() if isinstance(value, str) else value

    @field_validator("wholesaler_jids")
    @classmethod
    def clean_wholesaler_jids(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("Wholesaler JIDs must be unique")
        return cleaned

    @field_validator("wholesale_group_jids")
    @classmethod
    def clean_wholesale_group_jids(
        cls,
        values: list[str] | None,
    ) -> list[str] | None:
        if values is None:
            return None
        cleaned = [value.strip() for value in values if value.strip()]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("Wholesale group JIDs must be unique")
        return cleaned

    @model_validator(mode="after")
    def floor_must_not_exceed_listed_price(self) -> "BusinessSettingsUpdate":
        if (
            self.min_profit_rule is not None
            and "markup_rule" in self.model_fields_set
            and self.min_profit_rule > self.markup_rule
        ):
            raise ValueError("Minimum profit cannot exceed the standard markup")
        return self


class BusinessSettingsRead(BusinessSettingsUpdate):
    model_config = ConfigDict(from_attributes=True)

    updated_at: datetime


class ConversationUpdate(BaseModel):
    bot_muted: bool


class AdminMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class WholesaleRetailSandboxRequest(BaseModel):
    caption: str = Field(min_length=1, max_length=4000)
    previous_priced_caption: str | None = Field(default=None, max_length=4000)
    previous_image_base64: str | None = Field(default=None, max_length=20_000_000)
    previous_image_mime_type: str = "image/jpeg"
    image_base64: str | None = Field(default=None, max_length=20_000_000)
    image_mime_type: str = "image/jpeg"


class InquiryUpdate(BaseModel):
    status: OrderInquiryStatus


