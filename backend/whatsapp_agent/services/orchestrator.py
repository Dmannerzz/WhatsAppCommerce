import asyncio
import base64
import random
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from whatsapp_agent.config import get_settings
from whatsapp_agent.db import AsyncSessionFactory
from whatsapp_agent.models import (
    CustomerConversation,
    OrderInquiry,
    OrderInquiryStatus,
    Product,
)
from whatsapp_agent.schemas_webhooks import (
    CustomerMessagePayload,
    MediaPayload,
    WholesalerMessagePayload,
)
from whatsapp_agent.services.bridge_client import BridgeClient
from whatsapp_agent.services.llm_agent import LLMAgent, NegotiationIntent, StockAvailability


class Orchestrator:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.settings = get_settings()
        self.bridge = BridgeClient()
        self.llm = LLMAgent()

    async def handle_wholesale(self, payload: MediaPayload) -> dict[str, Any]:
        extracted = await self.llm.extract_caption(payload.caption or "")
        if extracted.wholesale_price is None:
            return {
                "accepted": False,
                "reason": "Wholesale price could not be extracted",
            }

        media_dir = Path("media")
        media_dir.mkdir(parents=True, exist_ok=True)
        try:
            image_bytes = base64.b64decode(payload.image_base64, validate=True)
        except ValueError as error:
            raise ValueError("image_base64 is not valid base64") from error
        suffix = ".jpg"
        if "png" in payload.mime_type:
            suffix = ".png"
        image_path = media_dir / f"wholesale_{payload.message_id.replace('/', '_')}{suffix}"
        image_path.write_bytes(image_bytes)

        wholesale_price = Decimal(extracted.wholesale_price)
        listed_price = wholesale_price + Decimal(self.settings.markup_rule)
        floor_price = wholesale_price + Decimal(self.settings.min_profit_rule)

        product = Product(
            wholesale_msg_id=payload.message_id,
            wholesaler_jid=payload.from_jid,
            image_path=str(image_path.resolve()),
            raw_caption=payload.caption or "",
            category=extracted.category,
            available_metadata={
                "sizes": extracted.sizes,
                "colors": extracted.colors,
                "product_name": extracted.product_name,
            },
            wholesale_price=wholesale_price,
            listed_price=listed_price,
            floor_price=floor_price,
        )
        self.session.add(product)
        await self.session.flush()

        caption = await self._rewrite_retail_caption(product)
        await self.session.commit()
        asyncio.create_task(self._jittered_repost(product.id, caption))
        return {"accepted": True, "product_id": str(product.id)}

    async def handle_customer_message(
        self,
        payload: CustomerMessagePayload,
    ) -> dict[str, Any]:
        conversation = await self._get_or_create_conversation(payload)
        if conversation.bot_muted:
            return {"handled": False, "reason": "bot_muted"}

        product = await self._find_product_for_customer(payload)
        if product is None:
            await self.bridge.send_typing(payload.chat_jid)
            await self.bridge.send_text(
                payload.chat_jid,
                "Hi dear! Which item are you asking about? You can quote the post.",
            )
            return {"handled": True, "reason": "product_not_found"}

        inquiry = await self._get_or_create_inquiry(conversation, product)
        metadata = product.available_metadata or {}
        below_floor_count = sum(
            1
            for turn in conversation.chat_history
            if turn.get("customer_offered_price") is not None
            and Decimal(str(turn["customer_offered_price"])) < product.floor_price
        )

        await self.bridge.send_typing(payload.chat_jid)
        result = await self.llm.negotiate(
            customer_name=conversation.customer_name,
            customer_message=payload.text,
            history=conversation.chat_history,
            product={
                "product_name": metadata.get("product_name") or "fashion item",
                "listed_price": str(product.listed_price),
                "floor_price": str(product.floor_price),
                "category": product.category,
                "sizes": metadata.get("sizes", []),
                "colors": metadata.get("colors", []),
            },
        )

        if result.extracted_size:
            inquiry.requested_size = result.extracted_size
        if result.extracted_color:
            inquiry.requested_color = result.extracted_color
        if result.customer_offered_price is not None:
            inquiry.agreed_price = result.customer_offered_price

        offer_below_floor = (
            result.customer_offered_price is not None
            and result.customer_offered_price < product.floor_price
        )
        if offer_below_floor:
            if below_floor_count >= 1:
                result = result.model_copy(
                    update={
                        "intent": NegotiationIntent.NEEDS_HUMAN_HANDOFF,
                        "handoff_reason": (
                            "Customer made repeated offers below the acceptable "
                            "price."
                        ),
                        "reply_text": (
                            "Let me check with Mum and get back to you, dear."
                        ),
                    }
                )
            else:
                result = result.model_copy(
                    update={
                        "intent": NegotiationIntent.NEGOTIATING,
                        "handoff_reason": None,
                        "reply_text": (
                            "I can't do that price, dear. Would you like to "
                            "continue at the listed price?"
                        ),
                    }
                )

        if result.intent == NegotiationIntent.NEEDS_HUMAN_HANDOFF:
            await self._handoff(conversation, inquiry, product, result.handoff_reason)
        elif result.intent == NegotiationIntent.READY_TO_CHECK_STOCK:
            await self._start_stock_check(conversation, inquiry, product)
        else:
            await self.bridge.send_text(
                payload.chat_jid,
                result.reply_text,
                quoted_message_id=payload.message_id,
            )
            inquiry.status = OrderInquiryStatus.NEGOTIATING

        conversation.chat_history = (
            conversation.chat_history + [
                {"role": "customer", "content": payload.text},
                {
                    "role": "assistant",
                    "content": result.reply_text,
                    "intent": result.intent.value,
                    "customer_offered_price": (
                        str(result.customer_offered_price)
                        if result.customer_offered_price is not None
                        else None
                    ),
                },
            ]
        )[-20:]
        await self.session.commit()
        return {"handled": True, "intent": result.intent.value}

    async def handle_wholesaler_message(
        self,
        payload: WholesalerMessagePayload,
    ) -> dict[str, Any]:
        result = await self.session.execute(
            select(OrderInquiry)
            .join(Product)
            .where(
                OrderInquiry.status == OrderInquiryStatus.PENDING_WHOLESALER,
                Product.wholesaler_jid == payload.from_jid,
            )
        )
        inquiries = list(result.scalars().all())
        if not inquiries:
            return {"handled": False, "reason": "no_pending_inquiry"}

        inquiry = inquiries[0]
        product = await self.session.get(Product, inquiry.product_id)
        if product is None:
            return {"handled": False, "reason": "product_missing"}
        classification = await self.llm.classify_stock(
            payload.text,
            product_name=(product.available_metadata or {}).get("product_name")
            or "fashion item",
            color=inquiry.requested_color,
            size=inquiry.requested_size,
        )

        conversation = await self.session.scalar(
            select(CustomerConversation).where(
                CustomerConversation.customer_jid == inquiry.customer_jid
            )
        )
        if conversation is None:
            return {"handled": False, "reason": "conversation_missing"}

        if classification.availability == StockAvailability.AVAILABLE:
            inquiry.status = OrderInquiryStatus.CONFIRMED_AVAILABLE
            await self.bridge.send_text(
                inquiry.customer_jid,
                "Good news! Your "
                f"{inquiry.requested_color or 'item'} in size "
                f"{inquiry.requested_size or 'the requested size'} is available. "
                "Should I lock it down for you?",
            )
            await self._notify_admin(
                f"Stock confirmed for {conversation.customer_name}: "
                f"{inquiry.requested_color or 'item'}, "
                f"size {inquiry.requested_size or 'unspecified'}."
            )
        elif classification.availability == StockAvailability.OUT_OF_STOCK:
            inquiry.status = OrderInquiryStatus.OUT_OF_STOCK
            product.is_in_stock = False
            alternatives = await self._find_alternatives(product)
            await self.bridge.send_text(
                inquiry.customer_jid,
                "Sorry, that exact size/color has just finished. "
                "I found two similar alternatives for you:",
            )
            for alternative in alternatives:
                await self.bridge.send_media(
                    inquiry.customer_jid,
                    alternative.image_path,
                    f"{alternative.category.title()} - ₦{alternative.listed_price}",
                )
        await self.session.commit()
        return {"handled": True, "availability": classification.availability.value}

    async def _rewrite_retail_caption(self, product: Product) -> str:
        metadata = product.available_metadata or {}
        details = []
        if metadata.get("colors"):
            details.append("Colours: " + ", ".join(metadata["colors"]))
        if metadata.get("sizes"):
            details.append("Sizes: " + ", ".join(metadata["sizes"]))
        return (
            f"✨ {metadata.get('product_name') or product.category.title()} ✨\n"
            f"Price: ₦{product.listed_price}\n"
            + ("\n".join(details) + "\n" if details else "")
            + "Send a message to check availability!"
        )

    async def _jittered_repost(self, product_id: UUID, caption: str) -> None:
        await asyncio.sleep(random.randint(120, 480))
        async with AsyncSessionFactory() as session:
            product = await session.get(Product, product_id)
            if product is None or not product.is_in_stock:
                return
            if self.settings.retail_group_jid is None:
                return
            result = await self.bridge.send_media(
                self.settings.retail_group_jid,
                product.image_path,
                caption,
            )
            product.retail_msg_id = result.get("messageId")
            await session.commit()

    async def _get_or_create_conversation(
        self,
        payload: CustomerMessagePayload,
    ) -> CustomerConversation:
        conversation = await self.session.scalar(
            select(CustomerConversation).where(
                CustomerConversation.customer_jid == payload.from_jid
            )
        )
        if conversation is None:
            conversation = CustomerConversation(
                customer_jid=payload.from_jid,
                customer_name=payload.sender_name or "Customer",
                chat_history=[],
            )
            self.session.add(conversation)
            await self.session.flush()
        return conversation

    async def _find_product_for_customer(
        self,
        payload: CustomerMessagePayload,
    ) -> Product | None:
        if payload.quoted_message_id:
            product = await self.session.scalar(
                select(Product).where(
                    Product.retail_msg_id == payload.quoted_message_id
                )
            )
            if product is not None:
                return product
        inquiry = await self.session.scalar(
            select(OrderInquiry)
            .join(CustomerConversation)
            .where(
                CustomerConversation.customer_jid == payload.from_jid,
            )
            .order_by(OrderInquiry.updated_at.desc())
        )
        if inquiry is not None:
            return await self.session.get(Product, inquiry.product_id)
        return None

    async def _get_or_create_inquiry(
        self,
        conversation: CustomerConversation,
        product: Product,
    ) -> OrderInquiry:
        inquiry = await self.session.scalar(
            select(OrderInquiry).where(
                OrderInquiry.customer_jid == conversation.customer_jid,
                OrderInquiry.product_id == product.id,
            )
            .order_by(OrderInquiry.updated_at.desc())
            .limit(1)
        )
        if inquiry is None:
            inquiry = OrderInquiry(
                customer_jid=conversation.customer_jid,
                product_id=product.id,
            )
            self.session.add(inquiry)
            await self.session.flush()
        return inquiry

    async def _handoff(
        self,
        conversation: CustomerConversation,
        inquiry: OrderInquiry,
        product: Product,
        reason: str | None,
    ) -> None:
        conversation.bot_muted = True
        inquiry.status = OrderInquiryStatus.HANDOFF_TO_MUM
        await self.bridge.send_text(
            conversation.customer_jid,
            "Let me involve Mum so we can sort this out properly for you.",
        )
        await self._notify_admin(
            f"Handoff requested for {conversation.customer_name}. "
            f"Product: {product.id}. Reason: {reason or 'No reason supplied'}."
        )

    async def _start_stock_check(
        self,
        conversation: CustomerConversation,
        inquiry: OrderInquiry,
        product: Product,
    ) -> None:
        if not inquiry.requested_size or not inquiry.requested_color:
            await self.bridge.send_text(
                conversation.customer_jid,
                "Please confirm both the exact size and colour so I can check.",
            )
            return
        inquiry.status = OrderInquiryStatus.PENDING_WHOLESALER
        await self.bridge.send_text(
            conversation.customer_jid,
            "Let me quickly confirm from the store if your exact size/color "
            "is still left, give me a few minutes!",
        )
        if self.settings.wholesale_group_jid:
            response = await self.bridge.send_text(
                product.wholesaler_jid,
                "Hello, please is this available in "
                f"{inquiry.requested_color}, Size {inquiry.requested_size}?",
                quoted_message_id=product.wholesale_msg_id,
            )
            inquiry.wholesaler_query_msg_id = response.get("messageId")
        asyncio.create_task(
            self._wholesaler_timeout_reminder(inquiry.id)
        )

    async def _wholesaler_timeout_reminder(self, inquiry_id: UUID) -> None:
        await asyncio.sleep(30 * 60)
        async with AsyncSessionFactory() as session:
            inquiry = await session.get(OrderInquiry, inquiry_id)
            if inquiry is None or inquiry.status != OrderInquiryStatus.PENDING_WHOLESALER:
                return
            conversation = await session.scalar(
                select(CustomerConversation).where(
                    CustomerConversation.customer_jid == inquiry.customer_jid
                )
            )
            name = conversation.customer_name if conversation else inquiry.customer_jid
            await BridgeClient().send_text(
                get_settings().mum_admin_jid or "",
                f"Reminder: wholesaler has not replied for {name}. "
                f"Inquiry {inquiry_id}.",
            )

    async def _notify_admin(self, text: str) -> None:
        if self.settings.mum_admin_jid:
            await self.bridge.send_text(self.settings.mum_admin_jid, text)

    async def _find_alternatives(
        self,
        product: Product,
    ) -> list[Product]:
        result = await self.session.execute(
            select(Product)
            .where(
                Product.id != product.id,
                Product.category == product.category,
                Product.is_in_stock.is_(True),
                Product.retail_msg_id.is_not(None),
            )
            .order_by(
                (Product.listed_price - product.listed_price).abs()
            )
            .limit(2)
        )
        return list(result.scalars().all())
