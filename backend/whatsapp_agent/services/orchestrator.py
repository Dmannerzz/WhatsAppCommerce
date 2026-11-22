import asyncio
import base64
import logging
import mimetypes
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
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
from whatsapp_agent.services.business_settings import get_business_settings
from whatsapp_agent.services.llm_agent import (
    LLMAgent,
    has_explicit_price_signal,
    NegotiationIntent,
    NegotiationOutput,
    ProductImageMatch,
    StockAvailability,
)

logger = logging.getLogger(__name__)
IMAGE_MATCH_CONFIDENCE_THRESHOLD = 0.9


def apply_floor_offer_guardrail(
    result: NegotiationOutput,
    history: list[dict[str, Any]],
    floor_price: Decimal,
    listed_price: Decimal,
) -> NegotiationOutput:
    below_floor_count = sum(
        1
        for turn in history
        if turn.get("customer_offered_price") is not None
        and Decimal(str(turn["customer_offered_price"])) < floor_price
    )
    offer_below_floor = (
        result.customer_offered_price is not None
        and result.customer_offered_price < floor_price
    )
    if not offer_below_floor:
        return result
    if below_floor_count >= 1:
        return result.model_copy(
            update={
                "intent": NegotiationIntent.NEEDS_HUMAN_HANDOFF,
                "handoff_reason": (
                    "Customer made repeated offers below the acceptable price."
                ),
                "reply_text": "I understand. I'll ask Mum to review this and get back to you.",
            }
        )
    return result.model_copy(
        update={
            "intent": NegotiationIntent.NEGOTIATING,
            "handoff_reason": None,
            "reply_text": (
                "Thanks for the offer. I can't sell at that price, but the listed "
                f"price is ₦{listed_price}. Would you like to continue at that price?"
            ),
        }
    )


def build_retail_caption(
    *,
    product_name: str | None,
    listed_price: Decimal,
    sizes: list[str],
    colors: list[str],
    color_sizes: dict[str, list[str]] | None = None,
) -> str:
    lines = []
    if product_name:
        lines.append(product_name)
    lines.append(f"Price: ₦{listed_price}")
    if color_sizes:
        lines.extend(
            f"{color}: {', '.join(available_sizes)}"
            for color, available_sizes in color_sizes.items()
            if available_sizes
        )
    else:
        if colors:
            lines.append("Colours: " + ", ".join(colors))
        if sizes:
            lines.append("Sizes: " + ", ".join(sizes))
    return "\n".join(lines)


class Orchestrator:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.bridge = BridgeClient()
        self.llm = LLMAgent()

    async def handle_wholesale(self, payload: MediaPayload) -> dict[str, Any]:
        business = await get_business_settings(self.session)
        if not business.automation_enabled:
            return {"accepted": False, "reason": "automation_paused"}
        if payload.chat_jid == business.retail_group_jid:
            return {"accepted": False, "reason": "unexpected_wholesale_group"}
        wholesale_group_jids = business.wholesale_group_jids or (
            [business.wholesale_group_jid]
            if business.wholesale_group_jid
            else []
        )
        if wholesale_group_jids and payload.chat_jid not in wholesale_group_jids:
            return {"accepted": False, "reason": "unexpected_wholesale_group"}
        existing = await self.session.scalar(
            select(Product).where(Product.wholesale_msg_id == payload.message_id)
        )
        if existing is not None:
            return {"accepted": True, "product_id": str(existing.id), "duplicate": True}
        previous_product = await self._recent_product_from_sender(payload.from_jid)
        extracted = await self.llm.extract_caption(payload.caption or "")
        if extracted.wholesale_price is None:
            if has_explicit_price_signal(payload.caption or ""):
                return {
                    "accepted": False,
                    "reason": (
                        "A price was present but could not be validated as an "
                        "eligible single-unit price; product was not posted"
                    ),
                }
            if previous_product is not None:
                try:
                    previous_image = await self._read_product_image(
                        previous_product.image_path
                    )
                except (OSError, httpx.HTTPError, ValueError) as error:
                    logger.warning(
                        "Could not read recent product image for variant comparison: %s",
                        error,
                    )
                    return {
                        "accepted": False,
                        "reason": "Recent product image was unavailable for comparison",
                    }
                candidate_image = await self._read_wholesale_image(payload)
                previous_mime_type = (
                    (previous_product.available_metadata or {}).get(
                        "image_mime_type"
                    )
                    or mimetypes.guess_type(
                        urlparse(previous_product.image_path).path
                    )[0]
                    or "image/jpeg"
                )
                comparison = await self.llm.compare_product_images(
                    previous_image,
                    previous_mime_type,
                    candidate_image,
                    payload.mime_type,
                )
                if (
                    comparison.decision == ProductImageMatch.SAME_PRODUCT
                    and comparison.confidence >= IMAGE_MATCH_CONFIDENCE_THRESHOLD
                ):
                    image_path = self._save_wholesale_image(
                        payload,
                        candidate_image,
                    )
                    if get_settings().bridge_transport == "database":
                        await self.bridge.schedule_retail_media(
                            "",
                            str(image_path),
                            payload.caption or "",
                            delay_seconds=random.randint(120, 480),
                            retail_variant=True,
                            session=self.session,
                        )
                        await self.session.commit()
                    else:
                        await self.session.commit()
                        asyncio.create_task(
                            self._jittered_repost_variant(
                                str(image_path),
                                payload.caption or "",
                            )
                        )
                    return {
                        "accepted": True,
                        "variant_followup": True,
                        "price_added": False,
                    }
            return {
                "accepted": False,
                "reason": (
                    "No eligible single-unit price was found and a same-product "
                    "image match could not be confirmed; product was not posted"
                ),
            }

        image_path = self._save_wholesale_image(payload)

        wholesale_price = Decimal(extracted.wholesale_price)
        listed_price = wholesale_price + business.markup_rule
        floor_price = wholesale_price + business.min_profit_rule

        product = Product(
            wholesale_msg_id=payload.message_id,
            wholesaler_jid=payload.from_jid,
            image_path=str(image_path),
            raw_caption=payload.caption or "",
            category=extracted.category,
            available_metadata={
                "sizes": extracted.sizes,
                "colors": extracted.colors,
                "color_sizes": extracted.color_sizes,
                "product_name": extracted.product_name,
                "image_mime_type": payload.mime_type,
            },
            wholesale_price=wholesale_price,
            listed_price=listed_price,
            floor_price=floor_price,
        )
        self.session.add(product)
        await self.session.flush()

        caption = await self._rewrite_retail_caption(product)
        if get_settings().bridge_transport == "database":
            await self.bridge.schedule_retail_media(
                "",
                str(image_path),
                caption,
                delay_seconds=random.randint(120, 480),
                product_id=product.id,
                session=self.session,
            )
        await self.session.commit()
        if get_settings().bridge_transport != "database":
            asyncio.create_task(self._jittered_repost(product.id, caption))
        return {"accepted": True, "product_id": str(product.id)}

    async def _recent_product_from_sender(self, sender_jid: str) -> Product | None:
        previous_product = await self.session.scalar(
            select(Product)
            .where(Product.wholesaler_jid == sender_jid)
            .order_by(Product.created_at.desc())
            .limit(1)
        )
        if previous_product is None:
            return None
        created_at = previous_product.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - created_at
        return (
            previous_product
            if timedelta(0) <= age <= timedelta(minutes=10)
            else None
        )

    @staticmethod
    def _decode_wholesale_image(payload: MediaPayload) -> bytes:
        if payload.image_base64 is None:
            raise ValueError("image_base64 is not present")
        try:
            return base64.b64decode(payload.image_base64, validate=True)
        except ValueError as error:
            raise ValueError("image_base64 is not valid base64") from error

    @staticmethod
    async def _read_product_image(image_path: str) -> bytes:
        if image_path.startswith("https://"):
            parsed = urlparse(image_path)
            if (
                parsed.hostname is None
                or not parsed.hostname.endswith(".public.blob.vercel-storage.com")
            ):
                raise ValueError("Product image is not stored in Vercel Blob")
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(image_path)
                response.raise_for_status()
                return response.content
        return Path(image_path).read_bytes()

    @staticmethod
    async def _read_wholesale_image(payload: MediaPayload) -> bytes:
        if payload.image_base64 is not None:
            return Orchestrator._decode_wholesale_image(payload)
        if payload.image_url is None:
            raise ValueError("Wholesale image source is missing")
        return await Orchestrator._read_product_image(str(payload.image_url))

    @staticmethod
    def _save_wholesale_image(
        payload: MediaPayload,
        image_bytes: bytes | None = None,
    ) -> Path | str:
        if payload.image_url is not None:
            return str(payload.image_url)
        media_dir = get_settings().media_dir
        media_dir.mkdir(parents=True, exist_ok=True)
        if image_bytes is None:
            image_bytes = Orchestrator._decode_wholesale_image(payload)
        suffix = mimetypes.guess_extension(payload.mime_type) or ".jpg"
        if suffix == ".jpe":
            suffix = ".jpg"
        image_path = media_dir / f"wholesale_{payload.message_id.replace('/', '_')}{suffix}"
        image_path.write_bytes(image_bytes)
        return image_path.resolve()

    async def handle_customer_message(
        self,
        payload: CustomerMessagePayload,
    ) -> dict[str, Any]:
        business = await get_business_settings(self.session)
        if not business.automation_enabled:
            return {"handled": False, "reason": "automation_paused"}
        pending_wholesaler_inquiry = await self._find_pending_wholesaler_inquiry(
            payload.from_jid,
            payload.quoted_message_id,
        )
        if pending_wholesaler_inquiry is not None:
            await self.session.commit()
            return await self.handle_wholesaler_message(
                WholesalerMessagePayload(
                    message_id=payload.message_id,
                    from_jid=payload.from_jid,
                    chat_jid=payload.chat_jid,
                    text=payload.text,
                    quoted_message_id=payload.quoted_message_id,
                    raw=payload.raw,
                )
            )
        if payload.from_jid in business.wholesaler_jids:
            await self.session.commit()
            return {"handled": False, "reason": "no_pending_inquiry"}
        known_wholesaler = await self.session.scalar(
            select(Product.id)
            .where(Product.wholesaler_jid == payload.from_jid)
            .limit(1)
        )
        if known_wholesaler is not None:
            await self.session.commit()
            return {"handled": False, "reason": "no_pending_inquiry"}

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
            await self.session.commit()
            return {"handled": True, "reason": "product_not_found"}

        inquiry = await self._get_or_create_inquiry(conversation, product)
        metadata = product.available_metadata or {}

        await self.bridge.send_typing(payload.chat_jid)
        result = await self.llm.negotiate(
            customer_name=conversation.customer_name,
            customer_message=payload.text,
            history=conversation.chat_history,
            product={
                "product_name": metadata.get("product_name") or "fashion item",
                "listed_price": str(product.listed_price),
                "floor_price": str(product.floor_price),
                "wholesale_price": str(product.wholesale_price),
                "category": product.category,
                "sizes": metadata.get("sizes", []),
                "colors": metadata.get("colors", []),
            },
        )
        result = apply_floor_offer_guardrail(
            result,
            conversation.chat_history,
            product.floor_price,
            product.listed_price,
        )

        if result.extracted_size:
            inquiry.requested_size = result.extracted_size
        if result.extracted_color:
            inquiry.requested_color = result.extracted_color
        if result.customer_offered_price is not None:
            inquiry.agreed_price = result.customer_offered_price

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
                *(
                    [OrderInquiry.wholesaler_query_msg_id == payload.quoted_message_id]
                    if payload.quoted_message_id
                    else []
                ),
            )
            .order_by(OrderInquiry.created_at.asc())
        )
        inquiries = list(result.scalars().all())
        if not payload.quoted_message_id and len(inquiries) != 1:
            return {
                "handled": False,
                "reason": (
                    "no_pending_inquiry"
                    if not inquiries
                    else "ambiguous_pending_inquiry"
                ),
            }
        if payload.quoted_message_id and not inquiries:
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
            business = await get_business_settings(self.session)
            payment_details = (
                f"\n\nPayment details: {business.payment_instructions}"
                if business.payment_instructions
                else ""
            )
            await self.bridge.send_text(
                inquiry.customer_jid,
                "Good news! Your "
                f"{inquiry.requested_color or 'item'} in size "
                f"{inquiry.requested_size or 'the requested size'} is available. "
                "Should I lock it down for you?"
                + payment_details,
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
        return build_retail_caption(
            product_name=metadata.get("product_name"),
            listed_price=product.listed_price,
            sizes=metadata.get("sizes", []),
            colors=metadata.get("colors", []),
            color_sizes=metadata.get("color_sizes"),
        )

    async def _jittered_repost(self, product_id: UUID, caption: str) -> None:
        await asyncio.sleep(random.randint(120, 480))
        async with AsyncSessionFactory() as session:
            business = await get_business_settings(session)
            product = await session.get(Product, product_id)
            if (
                product is None
                or not product.is_in_stock
                or not business.automation_enabled
                or not business.retail_group_jid
            ):
                return
            result = await self.bridge.send_media(
                business.retail_group_jid,
                product.image_path,
                caption,
            )
            product.retail_msg_id = result.get("messageId")
            await session.commit()

    async def _jittered_repost_variant(
        self,
        image_path: str,
        original_caption: str,
    ) -> None:
        await asyncio.sleep(random.randint(120, 480))
        async with AsyncSessionFactory() as session:
            business = await get_business_settings(session)
            if not business.automation_enabled or not business.retail_group_jid:
                return
            await self.bridge.send_media(
                business.retail_group_jid,
                image_path,
                original_caption,
            )

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
            business = await get_business_settings(session)
            if business.mum_admin_jid:
                await BridgeClient().send_text(
                    business.mum_admin_jid,
                    f"Reminder: wholesaler has not replied for {name}. "
                    f"Inquiry {inquiry_id}.",
                )

    async def _notify_admin(self, text: str) -> None:
        business = await get_business_settings(self.session)
        if business.mum_admin_jid:
            await self.bridge.send_text(business.mum_admin_jid, text)

    async def _find_pending_wholesaler_inquiry(
        self,
        wholesaler_jid: str,
        quoted_message_id: str | None,
    ) -> OrderInquiry | None:
        statement = (
            select(OrderInquiry)
            .join(Product)
            .where(
                OrderInquiry.status == OrderInquiryStatus.PENDING_WHOLESALER,
                Product.wholesaler_jid == wholesaler_jid,
            )
        )
        if quoted_message_id:
            statement = statement.where(
                OrderInquiry.wholesaler_query_msg_id == quoted_message_id
            )
            return await self.session.scalar(statement)
        result = await self.session.execute(statement)
        inquiries = list(result.scalars().all())
        return inquiries[0] if len(inquiries) == 1 else None

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
