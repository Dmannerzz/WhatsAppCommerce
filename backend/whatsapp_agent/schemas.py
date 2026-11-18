import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from whatsapp_agent.models import OrderInquiryStatus


class ProductCreate(BaseModel):
    wholesale_msg_id: str
    wholesaler_jid: str
    retail_msg_id: str | None = None
    image_path: str
    raw_caption: str
    category: str
    available_metadata: dict[str, Any] = Field(default_factory=dict)
    wholesale_price: Decimal
    listed_price: Decimal
    floor_price: Decimal
    is_in_stock: bool = True


class ProductRead(ProductCreate):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime


class CustomerConversationCreate(BaseModel):
    customer_jid: str
    customer_name: str
    bot_muted: bool = False
    chat_history: list[dict[str, Any]] = Field(default_factory=list)


class CustomerConversationRead(CustomerConversationCreate):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    updated_at: datetime


class OrderInquiryCreate(BaseModel):
    customer_jid: str
    product_id: uuid.UUID
    requested_color: str | None = None
    requested_size: str | None = None
    agreed_price: Decimal | None = None
    status: OrderInquiryStatus = OrderInquiryStatus.NEGOTIATING
    wholesaler_query_msg_id: str | None = None


class OrderInquiryRead(OrderInquiryCreate):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class OrderInquiryUpdate(BaseModel):
    requested_color: str | None = None
    requested_size: str | None = None
    agreed_price: Decimal | None = None
    status: OrderInquiryStatus | None = None
    wholesaler_query_msg_id: str | None = None
