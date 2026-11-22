import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from whatsapp_agent.config import get_settings
from whatsapp_agent.db import get_session
from whatsapp_agent.schemas_webhooks import MediaPayload
from whatsapp_agent.services.orchestrator import Orchestrator


router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def verify_webhook_secret(
    x_webhook_secret: str | None = Header(default=None),
) -> None:
    configured = get_settings().bridge_webhook_secret
    if configured is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook authentication is not configured",
        )
    expected = configured.get_secret_value()
    if len(expected) < 32 or expected.startswith("replace-with-"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook secret must be at least 32 characters",
        )
    if x_webhook_secret is None or not secrets.compare_digest(
        x_webhook_secret,
        expected,
    ):
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


@router.post(
    "/customer",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_webhook_secret)],
)
async def customer_webhook() -> dict[str, bool | str]:
    return {"handled": False, "reason": "customer_messaging_disabled"}


@router.post(
    "/wholesaler",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(verify_webhook_secret)],
)
async def wholesaler_webhook() -> dict[str, bool | str]:
    return {"handled": False, "reason": "customer_messaging_disabled"}
