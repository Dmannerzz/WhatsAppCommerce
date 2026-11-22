import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class OrderInquiryStatus(str, enum.Enum):
    NEGOTIATING = "NEGOTIATING"
    PENDING_WHOLESALER = "PENDING_WHOLESALER"
    CONFIRMED_AVAILABLE = "CONFIRMED_AVAILABLE"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    HANDOFF_TO_MUM = "HANDOFF_TO_MUM"
    COMPLETED = "COMPLETED"


jsonb_type = JSON().with_variant(JSONB(), "postgresql")


class BusinessSettings(Base):
    __tablename__ = "business_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    wholesale_group_jid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    wholesale_group_jids: Mapped[list[str]] = mapped_column(
        jsonb_type, nullable=False, default=list, server_default=text("'[]'")
    )
    retail_group_jid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mum_admin_jid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    wholesaler_jids: Mapped[list[str]] = mapped_column(
        jsonb_type, nullable=False, default=list
    )
    markup_rule: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("2500")
    )
    min_profit_rule: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("500")
    )
    payment_instructions: Mapped[str] = mapped_column(
        Text, nullable=False, default=""
    )
    automation_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        Index("ix_products_wholesale_msg_id", "wholesale_msg_id"),
        Index("ix_products_retail_msg_id", "retail_msg_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    wholesale_msg_id: Mapped[str] = mapped_column(String(255), nullable=False)
    wholesaler_jid: Mapped[str] = mapped_column(String(255), nullable=False)
    retail_msg_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    raw_caption: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    available_metadata: Mapped[dict[str, Any]] = mapped_column(
        jsonb_type, nullable=False, default=dict
    )
    wholesale_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    listed_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    floor_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    is_in_stock: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    inquiries: Mapped[list["OrderInquiry"]] = relationship(
        back_populates="product"
    )


class CustomerConversation(Base):
    __tablename__ = "customer_conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    customer_jid: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    customer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    bot_muted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    chat_history: Mapped[list[dict[str, Any]]] = mapped_column(
        jsonb_type, nullable=False, default=list
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    inquiries: Mapped[list["OrderInquiry"]] = relationship(
        back_populates="conversation"
    )


class OrderInquiry(Base):
    __tablename__ = "order_inquiries"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    customer_jid: Mapped[str] = mapped_column(
        ForeignKey("customer_conversations.customer_jid"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id"), nullable=False, index=True
    )
    requested_color: Mapped[str | None] = mapped_column(String(100), nullable=True)
    requested_size: Mapped[str | None] = mapped_column(String(100), nullable=True)
    agreed_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    status: Mapped[OrderInquiryStatus] = mapped_column(
        Enum(
            OrderInquiryStatus,
            name="order_inquiry_status",
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        nullable=False,
        default=OrderInquiryStatus.NEGOTIATING,
    )
    wholesaler_query_msg_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    conversation: Mapped[CustomerConversation] = relationship(
        back_populates="inquiries"
    )
    product: Mapped[Product] = relationship(back_populates="inquiries")


class BridgeCommand(Base):
    __tablename__ = "bridge_commands"
    __table_args__ = (
        Index("ix_bridge_commands_ready", "status", "available_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    command_type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(jsonb_type, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="queued", server_default="queued"
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(jsonb_type, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class BridgeRuntimeState(Base):
    __tablename__ = "bridge_runtime_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[dict[str, Any]] = mapped_column(jsonb_type, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class BridgeAuthFile(Base):
    __tablename__ = "bridge_auth_files"

    file_name: Mapped[str] = mapped_column(String(255), primary_key=True)
    contents: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
