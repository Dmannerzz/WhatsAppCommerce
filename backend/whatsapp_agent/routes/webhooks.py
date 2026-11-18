from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from whatsapp_agent.config import get_settings
from whatsapp_agent.db import get_session
from whatsapp_agent.schemas_webhooks import (
    CustomerMessagePayload,
    MediaPayload,
    WholesalerMessagePayload,
)
from whatsapp_agent.services.orchestrator import Orchestrator


router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def verify_webhook_secret(
    x_webhook_secret: str | None = Header(default=None),
) -> None:
    configured = get_settings().bridge_webhook_secret
    if configured is None:
        return
    if x_webhook_secret != configured.get_secret_value():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret",
        )


@router.post("/wholesale", dependencies=[Depends(verify_webhook_secret)])
async def wholesale_webhook(
    payload: MediaPayload,
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await Orchestrator(session).handle_wholesale(payload)


@router.post("/customer", dependencies=[Depends(verify_webhook_secret)])
async def customer_webhook(
    payload: CustomerMessagePayload,
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await Orchestrator(session).handle_customer_message(payload)


@router.post("/wholesaler", dependencies=[Depends(verify_webhook_secret)])
async def wholesaler_webhook(
    payload: WholesalerMessagePayload,
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await Orchestrator(session).handle_wholesaler_message(payload)
