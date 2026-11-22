import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from whatsapp_agent.config import get_settings
from whatsapp_agent.db import AsyncSessionFactory
from whatsapp_agent.models import BridgeCommand, BridgeRuntimeState


class BridgeClient:
    def __init__(self, base_url: str | None = None) -> None:
        settings = get_settings()
        self.transport = settings.bridge_transport
        self.base_url = (base_url or settings.bridge_api_url).rstrip("/")
        self.headers: dict[str, str] = {}
        if self.transport == "http":
            if settings.bridge_api_secret is None:
                raise RuntimeError("BRIDGE_API_SECRET is not configured")
            secret = settings.bridge_api_secret.get_secret_value()
            if len(secret) < 32:
                raise RuntimeError("BRIDGE_API_SECRET must be at least 32 characters")
            self.headers = {"x-bridge-secret": secret}

    async def _command(
        self,
        command_type: str,
        payload: dict[str, Any],
        *,
        timeout: float = 45,
        available_at: datetime | None = None,
    ) -> dict[str, Any]:
        async with AsyncSessionFactory() as session:
            command = BridgeCommand(
                command_type=command_type,
                payload=payload,
                available_at=available_at or datetime.now(timezone.utc),
            )
            session.add(command)
            await session.commit()
            command_id = command.id

        async def cancel_if_queued() -> None:
            async with AsyncSessionFactory() as session:
                await session.execute(
                    update(BridgeCommand)
                    .where(
                        BridgeCommand.id == command_id,
                        BridgeCommand.status == "queued",
                    )
                    .values(
                        status="cancelled",
                        error="Caller stopped waiting before the bridge started",
                    )
                )
                await session.commit()

        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                async with AsyncSessionFactory() as session:
                    command = await session.get(BridgeCommand, command_id)
                    if command is None:
                        raise RuntimeError(
                            "Bridge command disappeared before completion"
                        )
                    if command.status == "completed":
                        return command.result or {}
                    if command.status == "failed":
                        raise RuntimeError(
                            command.error or "WhatsApp bridge command failed"
                        )
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            await cancel_if_queued()
            raise
        await cancel_if_queued()
        raise RuntimeError("WhatsApp bridge did not complete the command in time")

    async def _enqueue_media(
        self,
        to: str,
        media_url: str,
        caption: str,
        *,
        quoted_message_id: str | None = None,
        product_id: UUID | None = None,
        retail_variant: bool = False,
        delay_seconds: int = 0,
        session: AsyncSession | None = None,
    ) -> None:
        parsed_url = urlparse(media_url)
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname is None
            or not parsed_url.hostname.endswith(".public.blob.vercel-storage.com")
        ):
            raise RuntimeError(
                "Vercel bridge media must be stored in a public Vercel Blob"
            )
        payload: dict[str, Any] = {
            "to": to,
            "mediaUrl": media_url,
            "caption": caption,
            "retailVariant": retail_variant,
            "deleteMediaAfterSend": retail_variant,
        }
        if quoted_message_id:
            payload["quotedMessageId"] = quoted_message_id
        if product_id:
            payload["productId"] = str(product_id)
        command = BridgeCommand(
            command_type="send_media",
            payload=payload,
            available_at=datetime.now(timezone.utc)
            + timedelta(seconds=delay_seconds),
        )
        if session is not None:
            session.add(command)
            return
        async with AsyncSessionFactory() as command_session:
            command_session.add(command)
            await command_session.commit()

    async def send_text(
        self,
        to: str,
        text: str,
        *,
        quoted_message_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"to": to, "text": text}
        if quoted_message_id:
            payload["quotedMessageId"] = quoted_message_id
        if self.transport == "database":
            return await self._command("send_text", payload)
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.base_url}/api/messages/send",
                json=payload,
                headers=self.headers,
            )
            response.raise_for_status()
            return response.json()

    async def send_media(
        self,
        to: str,
        media_path: str,
        caption: str,
        *,
        quoted_message_id: str | None = None,
    ) -> dict[str, Any]:
        if self.transport == "database":
            await self._enqueue_media(
                to,
                media_path,
                caption,
                quoted_message_id=quoted_message_id,
            )
            return {"queued": True}
        payload: dict[str, Any] = {
            "to": to,
            "mediaPath": media_path,
            "caption": caption,
        }
        if quoted_message_id:
            payload["quotedMessageId"] = quoted_message_id
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/api/messages/send-media",
                json=payload,
                headers=self.headers,
            )
            response.raise_for_status()
            return response.json()

    async def schedule_retail_media(
        self,
        to: str,
        media_url: str,
        caption: str,
        *,
        delay_seconds: int,
        product_id: UUID | None = None,
        retail_variant: bool = False,
        session: AsyncSession | None = None,
    ) -> None:
        if self.transport != "database":
            raise RuntimeError("Scheduled bridge commands require database transport")
        await self._enqueue_media(
            to,
            media_url,
            caption,
            product_id=product_id,
            retail_variant=retail_variant,
            delay_seconds=delay_seconds,
            session=session,
        )

    async def send_typing(self, to: str) -> None:
        if self.transport == "database":
            await self._command("send_typing", {"to": to})
            return
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{self.base_url}/api/messages/typing",
                json={"to": to},
                headers=self.headers,
            )

    async def get_status(self) -> dict[str, Any]:
        if self.transport == "database":
            async with AsyncSessionFactory() as session:
                runtime = await session.get(BridgeRuntimeState, 1)
                if runtime is None:
                    raise RuntimeError("WhatsApp bridge has not reported its status")
                updated_at = runtime.updated_at
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                if updated_at < datetime.now(timezone.utc) - timedelta(seconds=60):
                    raise RuntimeError("WhatsApp bridge status is stale")
                return runtime.state
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(
                f"{self.base_url}/health",
                headers=self.headers,
            )
            response.raise_for_status()
            return response.json()

    async def get_groups(self) -> list[dict[str, str]]:
        if self.transport == "database":
            result = await self._command("get_groups", {}, timeout=60)
            groups = result.get("groups")
            if not isinstance(groups, list):
                raise RuntimeError("WhatsApp bridge returned an invalid group list")
            return groups
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(
                f"{self.base_url}/api/groups",
                headers=self.headers,
            )
            response.raise_for_status()
            return response.json()["groups"]
