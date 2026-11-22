import base64
import secrets
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    status,
)
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from whatsapp_agent.config import get_settings
from whatsapp_agent.db import get_session
from whatsapp_agent.models import (
    BusinessSettings,
    CustomerConversation,
    OrderInquiry,
    OrderInquiryStatus,
    Product,
)
from whatsapp_agent.schemas_admin import (
    AdminMessage,
    BusinessSettingsRead,
    BusinessSettingsUpdate,
    ConversationUpdate,
    InquiryUpdate,
    WholesaleRetailSandboxRequest,
)
from whatsapp_agent.services.llm_agent import (
    LLMAgent,
    ProductImageMatch,
    has_explicit_price_signal,
)
from whatsapp_agent.services.orchestrator import (
    IMAGE_MATCH_CONFIDENCE_THRESHOLD,
    build_retail_caption,
)
from whatsapp_agent.services.bridge_client import BridgeClient
from whatsapp_agent.services.business_settings import get_business_settings


router = APIRouter(prefix="/api/admin", tags=["admin"])
security = HTTPBasic(auto_error=False)
dashboard_file = (
    Path(__file__).resolve().parents[1] / "static" / "admin.html"
)
sandbox_file = (
    Path(__file__).resolve().parents[1] / "static" / "sandbox.html"
)


def require_admin(
    credentials: HTTPBasicCredentials | None = Depends(security),
) -> str:
    configured = get_settings()
    if (
        configured.admin_password is None
        or len(configured.admin_password.get_secret_value()) < 16
        or configured.admin_password.get_secret_value().startswith("replace-with-")
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Set ADMIN_PASSWORD in the server environment before using the dashboard",
        )
    username_ok = secrets.compare_digest(
        credentials.username if credentials else "",
        configured.admin_username,
    )
    password_ok = secrets.compare_digest(
        credentials.password if credentials else "",
        configured.admin_password.get_secret_value(),
    )
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return configured.admin_username


def require_dashboard_request(
    x_dashboard_request: str | None = Header(default=None),
) -> None:
    if x_dashboard_request != "1":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dashboard request header required",
        )


@router.get(
    "",
    include_in_schema=False,
    dependencies=[Depends(require_admin)],
)
async def dashboard() -> FileResponse:
    return FileResponse(dashboard_file, media_type="text/html")


@router.get(
    "/sandbox",
    include_in_schema=False,
    dependencies=[Depends(require_admin)],
)
async def posting_sandbox() -> FileResponse:
    return FileResponse(sandbox_file, media_type="text/html")


@router.get(
    "/settings",
    response_model=BusinessSettingsRead,
    dependencies=[Depends(require_admin), Depends(require_dashboard_request)],
)
async def read_settings(
    session: AsyncSession = Depends(get_session),
) -> BusinessSettings:
    settings = await get_business_settings(session)
    await session.commit()
    return settings


@router.put(
    "/settings",
    response_model=BusinessSettingsRead,
    dependencies=[Depends(require_admin), Depends(require_dashboard_request)],
)
async def update_settings(
    payload: BusinessSettingsUpdate,
    session: AsyncSession = Depends(get_session),
) -> BusinessSettings:
    settings = await get_business_settings(session)
    updates = payload.model_dump(exclude_unset=True)
    if "wholesale_group_jids" in updates:
        candidate_group_jids = updates["wholesale_group_jids"] or []
    elif "wholesale_group_jid" in updates:
        candidate_group_jids = (
            [updates["wholesale_group_jid"]]
            if updates["wholesale_group_jid"]
            else []
        )
    else:
        candidate_group_jids = settings.wholesale_group_jids or (
            [settings.wholesale_group_jid]
            if settings.wholesale_group_jid
            else []
        )
    candidate_retail_group = updates.get(
        "retail_group_jid",
        settings.retail_group_jid,
    )
    if candidate_retail_group and candidate_retail_group in candidate_group_jids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The retail destination cannot also be a wholesale source",
        )
    if "wholesale_group_jids" in updates:
        group_jids = updates["wholesale_group_jids"] or []
        updates["wholesale_group_jids"] = group_jids
        updates["wholesale_group_jid"] = group_jids[0] if group_jids else None
    elif "wholesale_group_jid" in updates:
        legacy_group_jid = updates["wholesale_group_jid"]
        updates["wholesale_group_jids"] = (
            [legacy_group_jid] if legacy_group_jid else []
        )
    if (
        "min_profit_rule" in updates
        and updates["min_profit_rule"] > updates.get("markup_rule", settings.markup_rule)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Minimum profit cannot exceed the standard markup",
        )
    for key, value in updates.items():
        setattr(settings, key, value)
    await session.commit()
    await session.refresh(settings)
    return settings


@router.get(
    "/overview",
    dependencies=[Depends(require_admin), Depends(require_dashboard_request)],
)
async def overview(session: AsyncSession = Depends(get_session)) -> dict:
    settings = await get_business_settings(session)
    await session.commit()
    try:
        bridge = await BridgeClient().get_status()
    except (httpx.HTTPError, ValueError, RuntimeError):
        bridge = {"reachable": False, "connected": False, "qr_data_url": None}
    else:
        bridge["reachable"] = True

    stats = {
        "products": await session.scalar(select(func.count(Product.id))) or 0,
        "retail_posts": await session.scalar(
            select(func.count(Product.id)).where(Product.retail_msg_id.is_not(None))
        )
        or 0,
    }

    product_rows = (
        await session.execute(
            select(Product).order_by(Product.created_at.desc()).limit(30)
        )
    ).scalars()
    products = [
        {
            "id": str(product.id),
            "category": product.category,
            "product_name": (product.available_metadata or {}).get("product_name"),
            "listed_price": str(product.listed_price),
            "retail_msg_id": product.retail_msg_id,
            "created_at": product.created_at.isoformat(),
        }
        for product in product_rows
    ]

    return {
        "stats": stats,
        "bridge": bridge,
        "settings": {
            "wholesale_group_jids": settings.wholesale_group_jids,
            "retail_group_jid": settings.retail_group_jid,
            "markup_rule": str(settings.markup_rule),
            "automation_enabled": settings.automation_enabled,
        },
        "products": products,
    }


@router.post(
    "/sandbox/wholesale-retail",
    dependencies=[Depends(require_admin), Depends(require_dashboard_request)],
)
async def preview_wholesale_retail_message(
    payload: WholesaleRetailSandboxRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    business = await get_business_settings(session)
    agent = LLMAgent()
    extracted = await agent.extract_caption(payload.caption)
    if extracted.wholesale_price is None:
        if has_explicit_price_signal(payload.caption):
            return {
                "accepted": False,
                "reason": (
                    "A price was present but could not be validated as an eligible "
                    "single-unit price; the original caption was not previewed."
                ),
            }
        if payload.previous_priced_caption:
            if not payload.previous_image_base64 or not payload.image_base64:
                return {
                    "accepted": False,
                    "reason": (
                        "Upload both the previous and new product images to test "
                        "a variant follow-up."
                    ),
                }
            try:
                previous_image = base64.b64decode(
                    payload.previous_image_base64,
                    validate=True,
                )
                candidate_image = base64.b64decode(
                    payload.image_base64,
                    validate=True,
                )
            except ValueError as error:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Both sandbox images must be valid base64 data",
                ) from error
            comparison = await agent.compare_product_images(
                previous_image,
                payload.previous_image_mime_type,
                candidate_image,
                payload.image_mime_type,
            )
            if (
                comparison.decision == ProductImageMatch.SAME_PRODUCT
                and comparison.confidence >= IMAGE_MATCH_CONFIDENCE_THRESHOLD
            ):
                return {
                    "accepted": True,
                    "variant_followup": True,
                    "price_added": False,
                    "image_match_confidence": comparison.confidence,
                    "retail_caption": payload.caption,
                }
            return {
                "accepted": False,
                "reason": (
                    "The images were not a confident match; the variant was not "
                    "previewed."
                ),
                "image_match": comparison.decision.value,
                "image_match_confidence": comparison.confidence,
            }
        return {
            "accepted": False,
            "reason": (
                "No eligible single-unit wholesale price was found; "
                "product was not posted."
            ),
            "category": extracted.category,
            "product_name": extracted.product_name,
            "sizes": extracted.sizes,
            "colors": extracted.colors,
            "color_sizes": extracted.color_sizes,
        }

    listed_price = extracted.wholesale_price + business.markup_rule
    floor_price = extracted.wholesale_price + business.min_profit_rule
    retail_caption = build_retail_caption(
        product_name=extracted.product_name,
        listed_price=listed_price,
        sizes=extracted.sizes,
        colors=extracted.colors,
        color_sizes=extracted.color_sizes,
    )
    return {
        "accepted": True,
        "category": extracted.category,
        "product_name": extracted.product_name,
        "sizes": extracted.sizes,
        "colors": extracted.colors,
        "color_sizes": extracted.color_sizes,
        "wholesale_price": str(extracted.wholesale_price),
        "listed_price": str(listed_price),
        "floor_price": str(floor_price),
        "retail_caption": retail_caption,
    }


@router.get(
    "/groups",
    dependencies=[Depends(require_admin), Depends(require_dashboard_request)],
)
async def whatsapp_groups() -> dict[str, list[dict[str, str]]]:
    try:
        groups = await BridgeClient().get_groups()
    except httpx.HTTPStatusError as error:
        raise HTTPException(
            status_code=error.response.status_code,
            detail=error.response.text or "WhatsApp groups are unavailable",
        ) from error
    except (httpx.HTTPError, RuntimeError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Connect WhatsApp before loading groups",
        ) from error
    return {"groups": groups}


@router.get(
    "/bridge-status",
    dependencies=[Depends(require_admin), Depends(require_dashboard_request)],
)
async def bridge_status() -> dict:
    try:
        bridge = await BridgeClient().get_status()
    except (httpx.HTTPError, ValueError, RuntimeError):
        return {
            "reachable": False,
            "connected": False,
            "account_jid": None,
            "qr_data_url": None,
        }
    return {"reachable": True, **bridge}


@router.patch(
    "/conversations/{customer_jid}",
    dependencies=[Depends(require_admin), Depends(require_dashboard_request)],
)
async def update_conversation(
    customer_jid: str,
    payload: ConversationUpdate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    conversation = await session.scalar(
        select(CustomerConversation).where(
            CustomerConversation.customer_jid == customer_jid
        )
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conversation.bot_muted = payload.bot_muted
    await session.commit()
    return {"bot_muted": conversation.bot_muted}


@router.post(
    "/conversations/{customer_jid}/messages",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_admin), Depends(require_dashboard_request)],
)
async def send_customer_message(
    customer_jid: str,
    payload: AdminMessage,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    conversation = await session.scalar(
        select(CustomerConversation).where(
            CustomerConversation.customer_jid == customer_jid
        )
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        await BridgeClient().send_text(customer_jid, payload.text)
    except (httpx.HTTPError, RuntimeError) as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Message could not be sent through the WhatsApp bridge",
        ) from error
    conversation.chat_history = (
        conversation.chat_history
        + [{"role": "admin", "content": payload.text}]
    )[-20:]
    await session.commit()
    return {"status": "sent"}


@router.patch(
    "/inquiries/{inquiry_id}",
    dependencies=[Depends(require_admin), Depends(require_dashboard_request)],
)
async def update_inquiry(
    inquiry_id: UUID,
    payload: InquiryUpdate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    inquiry = await session.get(OrderInquiry, inquiry_id)
    if inquiry is None:
        raise HTTPException(status_code=404, detail="Inquiry not found")
    inquiry.status = payload.status
    conversation = await session.scalar(
        select(CustomerConversation).where(
            CustomerConversation.customer_jid == inquiry.customer_jid
        )
    )
    if (
        conversation is not None
        and payload.status == OrderInquiryStatus.HANDOFF_TO_MUM
    ):
        conversation.bot_muted = True
    product = await session.get(Product, inquiry.product_id)
    if product is not None and payload.status == OrderInquiryStatus.OUT_OF_STOCK:
        product.is_in_stock = False
    elif product is not None and payload.status == OrderInquiryStatus.CONFIRMED_AVAILABLE:
        product.is_in_stock = True
    await session.commit()
    return {"status": inquiry.status.value}


@router.post(
    "/automation/{action}",
    dependencies=[Depends(require_admin), Depends(require_dashboard_request)],
)
async def set_automation(
    action: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, bool]:
    if action not in {"pause", "resume"}:
        raise HTTPException(status_code=404, detail="Unknown automation action")
    settings = await get_business_settings(session)
    settings.automation_enabled = action == "resume"
    await session.commit()
    return {"automation_enabled": settings.automation_enabled}


