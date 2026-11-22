import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, status

from whatsapp_agent.config import get_settings
from whatsapp_agent.db import AsyncSessionFactory
from whatsapp_agent.services.business_settings import get_business_settings


router = APIRouter(prefix="/api/bridge", tags=["bridge"])


def verify_bridge_secret(x_bridge_secret: str | None = Header(default=None)) -> None:
    configured = get_settings().bridge_api_secret
    if configured is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bridge API authentication is not configured",
        )
    expected = configured.get_secret_value()
    if len(expected) < 32 or expected.startswith("replace-with-"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bridge API secret must be at least 32 characters",
        )
    if x_bridge_secret is None or not secrets.compare_digest(
        x_bridge_secret,
        expected,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bridge API secret",
        )


@router.get("/config", dependencies=[Depends(verify_bridge_secret)])
async def bridge_config() -> dict[str, list[str] | str | None]:
    async with AsyncSessionFactory() as session:
        settings = await get_business_settings(session)
        await session.commit()
        group_jids = settings.wholesale_group_jids or (
            [settings.wholesale_group_jid]
            if settings.wholesale_group_jid
            else []
        )
        return {
            "wholesale_group_jids": group_jids,
            "wholesale_group_jid": (
                group_jids[0] if group_jids else None
            ),
        }
