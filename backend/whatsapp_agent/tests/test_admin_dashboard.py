import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from decimal import Decimal
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from whatsapp_agent.app import app
from whatsapp_agent.config import get_settings
from whatsapp_agent.db import async_database_url, get_session
from whatsapp_agent.models import (
    Base,
    BridgeCommand,
    BusinessSettings,
    CustomerConversation,
    OrderInquiry,
    OrderInquiryStatus,
    Product,
)
from whatsapp_agent.schemas_webhooks import MediaPayload
from whatsapp_agent.services.llm_agent import (
    CaptionExtraction,
    LLMAgent,
    LLMProviderError,
    ProductImageComparison,
    ProductImageMatch,
)
from whatsapp_agent.services.bridge_client import BridgeClient
from whatsapp_agent.services.orchestrator import (
    Orchestrator,
    build_retail_caption,
)


def test_render_postgres_urls_use_asyncpg_driver():
    assert async_database_url(
        "postgresql://user:password@host/database"
    ) == "postgresql+asyncpg://user:password@host/database"
    assert async_database_url(
        "postgres://user:password@host/database"
    ) == "postgresql+asyncpg://user:password@host/database"
    assert async_database_url(
        "postgresql+asyncpg://user:password@host/database"
    ) == "postgresql+asyncpg://user:password@host/database"


def test_wholesale_payload_accepts_remote_vercel_blob_image():
    payload = MediaPayload(
        message_id="remote-image",
        from_jid="seller@s.whatsapp.net",
        chat_jid="wholesale@g.us",
        image_url="https://store.public.blob.vercel-storage.com/item.jpg",
    )

    assert str(payload.image_url) == (
        "https://store.public.blob.vercel-storage.com/item.jpg"
    )
    assert payload.image_base64 is None


@pytest.mark.parametrize(
    "image_sources",
    [
        {},
        {
            "image_base64": "aW1hZ2U=",
            "image_url": "https://store.public.blob.vercel-storage.com/item.jpg",
        },
    ],
)
def test_wholesale_payload_requires_exactly_one_image_source(image_sources):
    with pytest.raises(ValidationError):
        MediaPayload(
            message_id="invalid-image",
            from_jid="seller@s.whatsapp.net",
            chat_jid="wholesale@g.us",
            **image_sources,
        )


@pytest.mark.asyncio
async def test_remote_product_images_must_be_from_vercel_blob():
    with pytest.raises(ValueError, match="Vercel Blob"):
        await Orchestrator._read_product_image("https://example.com/image.jpg")


@pytest.mark.asyncio
async def test_vercel_posting_is_queued_with_randomized_delay(
    admin_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BRIDGE_TRANSPORT", "database")
    get_settings.cache_clear()

    async def fake_extract_caption(self, caption: str):
        return CaptionExtraction(
            wholesale_price=Decimal("15000"),
            category="shoe",
            sizes=["38"],
            colors=["Black"],
        )

    monkeypatch.setattr(LLMAgent, "extract_caption", fake_extract_caption)
    monkeypatch.setattr(
        Orchestrator,
        "_save_wholesale_image",
        staticmethod(
            lambda payload, image_bytes=None:
                "https://store.public.blob.vercel-storage.com/item.jpg"
        ),
    )
    started_at = datetime.now(timezone.utc)
    async with app.state.test_sessions() as session:
        session.add(
            BusinessSettings(
                id=1,
                automation_enabled=True,
                wholesale_group_jid="wholesale@g.us",
                retail_group_jid="retail@g.us",
            )
        )
        await session.commit()

        result = await Orchestrator(session).handle_wholesale(
            MediaPayload(
                message_id="vercel-post",
                from_jid="seller@s.whatsapp.net",
                chat_jid="wholesale@g.us",
                caption="Shoe, ₦15,000",
                image_base64="aW1hZ2U=",
            )
        )
        command = await session.scalar(
            select(BridgeCommand).where(
                BridgeCommand.command_type == "send_media"
            )
        )

    assert result["accepted"] is True
    assert command is not None
    scheduled_at = command.available_at
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    delay = scheduled_at - started_at
    assert timedelta(seconds=120) <= delay <= timedelta(seconds=480)
    assert command.payload["productId"] == result["product_id"]


def test_retail_caption_does_not_use_category_as_title():
    caption = build_retail_caption(
        product_name=None,
        listed_price=Decimal("15700"),
        colors=["Gold"],
        sizes=["37", "38"],
    )

    assert caption == (
        "Price: ₦15700\nColours: Gold\nSizes: 37, 38"
    )


def test_retail_caption_keeps_sizes_with_their_colors():
    caption = build_retail_caption(
        product_name=None,
        listed_price=Decimal("19000"),
        colors=["Gold", "Black", "Wine"],
        sizes=["38", "41", "37", "40"],
        color_sizes={
            "Gold": ["38", "41"],
            "Black": ["37"],
            "Wine": ["40"],
        },
    )

    assert caption == (
        "Price: ₦19000\nGold: 38, 41\nBlack: 37\nWine: 40"
    )


@pytest_asyncio.fixture
async def admin_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("ADMIN_USERNAME", "shop-owner")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-very-long-local-admin-password")
    monkeypatch.setenv("BRIDGE_API_SECRET", "x" * 40)
    monkeypatch.setenv("BRIDGE_WEBHOOK_SECRET", "y" * 40)
    get_settings.cache_clear()

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app.state.test_sessions = sessions

    async def override_session():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        auth=("shop-owner", "a-very-long-local-admin-password"),
        headers={"X-Dashboard-Request": "1"},
    ) as client:
        yield client
    app.dependency_overrides.clear()
    del app.state.test_sessions
    get_settings.cache_clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_dashboard_requires_credentials(admin_client: httpx.AsyncClient):
    response = await admin_client.get(
        "/api/admin/settings",
        auth=("shop-owner", "wrong-password"),
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_dashboard_page_is_served_to_authenticated_admin(
    admin_client: httpx.AsyncClient,
):
    response = await admin_client.get("/api/admin")
    assert response.status_code == 200
    assert "Business settings" in response.text
    assert "Getting started" in response.text
    assert "Test a caption" in response.text
    assert "wholesaleCaption" not in response.text
    assert "Order inquiries" not in response.text
    assert "Customer conversations" not in response.text


@pytest.mark.asyncio
async def test_posting_sandbox_is_a_separate_authenticated_page(
    admin_client: httpx.AsyncClient,
):
    response = await admin_client.get("/api/admin/sandbox")

    assert response.status_code == 200
    assert "Posting sandbox" in response.text
    assert "wholesaleCaption" in response.text
    assert "Back to dashboard" in response.text


@pytest.mark.asyncio
async def test_dashboard_can_load_whatsapp_groups(
    admin_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_get_groups(self):
        return [
            {"jid": "wholesale@g.us", "subject": "Wholesale"},
            {"jid": "retail@g.us", "subject": "Retail"},
        ]

    monkeypatch.setattr(BridgeClient, "get_groups", fake_get_groups)
    response = await admin_client.get("/api/admin/groups")
    assert response.status_code == 200
    assert response.json()["groups"][0]["jid"] == "wholesale@g.us"


@pytest.mark.asyncio
async def test_dashboard_saves_multiple_wholesale_groups(
    admin_client: httpx.AsyncClient,
):
    response = await admin_client.put(
        "/api/admin/settings",
        json={
            "wholesale_group_jids": ["first@g.us", "second@g.us"],
            "retail_group_jid": "retail@g.us",
            "markup_rule": "2500",
            "automation_enabled": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["wholesale_group_jids"] == ["first@g.us", "second@g.us"]
    assert response.json()["wholesale_group_jid"] == "first@g.us"


@pytest.mark.asyncio
async def test_dashboard_rejects_retail_group_as_wholesale_source(
    admin_client: httpx.AsyncClient,
):
    response = await admin_client.put(
        "/api/admin/settings",
        json={
            "wholesale_group_jids": ["retail@g.us"],
            "retail_group_jid": "retail@g.us",
            "markup_rule": "2500",
            "automation_enabled": True,
        },
    )

    assert response.status_code == 422
    assert "cannot also be a wholesale source" in response.json()["detail"]


@pytest.mark.asyncio
async def test_bridge_config_exposes_all_selected_wholesale_groups(
    admin_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    from whatsapp_agent.routes import bridge as bridge_routes

    monkeypatch.setattr(
        bridge_routes,
        "AsyncSessionFactory",
        app.state.test_sessions,
    )
    async with app.state.test_sessions() as session:
        session.add(
            BusinessSettings(
                id=1,
                wholesale_group_jid="first@g.us",
                wholesale_group_jids=["first@g.us", "second@g.us"],
                markup_rule=Decimal("2500"),
                min_profit_rule=Decimal("500"),
            )
        )
        await session.commit()

    response = await admin_client.get(
        "/api/bridge/config",
        headers={"x-bridge-secret": "x" * 40},
    )

    assert response.status_code == 200
    assert response.json()["wholesale_group_jids"] == [
        "first@g.us",
        "second@g.us",
    ]


@pytest.mark.asyncio
async def test_dashboard_overview_only_returns_posting_data(
    admin_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_get_status(self):
        return {
            "connected": True,
            "connection_state": "connected",
            "account_jid": "shop@s.whatsapp.net",
            "qr_data_url": None,
        }

    monkeypatch.setattr(BridgeClient, "get_status", fake_get_status)
    async with app.state.test_sessions() as session:
        session.add(
            Product(
                wholesale_msg_id="source-post",
                wholesaler_jid="seller@s.whatsapp.net",
                retail_msg_id="retail-post",
                image_path="media/item.jpg",
                raw_caption="Shoe, ₦10,000",
                category="shoe",
                available_metadata={"product_name": "Shoe"},
                wholesale_price=Decimal("10000"),
                listed_price=Decimal("12500"),
                floor_price=Decimal("10500"),
            )
        )
        await session.commit()

    response = await admin_client.get("/api/admin/overview")

    assert response.status_code == 200
    result = response.json()
    assert result["stats"] == {"products": 1, "retail_posts": 1}
    assert "inquiries" not in result
    assert "conversations" not in result
    assert "payment_instructions" not in result["settings"]
    assert "wholesaler_jids" not in result["settings"]
    assert result["products"][0]["product_name"] == "Shoe"
    assert Decimal(result["products"][0]["listed_price"]) == Decimal("12500")
    assert not {
        "wholesale_price",
        "floor_price",
        "image_path",
    }.intersection(result["products"][0])


@pytest.mark.asyncio
async def test_customer_and_wholesaler_webhooks_are_disabled(
    admin_client: httpx.AsyncClient,
):
    headers = {"X-Webhook-Secret": "y" * 40}
    customer_response = await admin_client.post(
        "/api/webhooks/customer",
        headers=headers,
        json={
            "message_id": "customer-message",
            "from_jid": "customer@s.whatsapp.net",
            "chat_jid": "customer@s.whatsapp.net",
            "text": "Is this available?",
        },
    )
    wholesaler_response = await admin_client.post(
        "/api/webhooks/wholesaler",
        headers=headers,
        json={
            "message_id": "supplier-message",
            "from_jid": "seller@s.whatsapp.net",
            "chat_jid": "seller@s.whatsapp.net",
            "text": "Available in black.",
        },
    )

    assert customer_response.status_code == 202
    assert customer_response.json()["handled"] is False
    assert wholesaler_response.status_code == 202
    assert wholesaler_response.json()["handled"] is False
    async with app.state.test_sessions() as session:
        assert await session.scalar(select(CustomerConversation.customer_jid)) is None
        assert await session.scalar(select(OrderInquiry.id)) is None


@pytest.mark.asyncio
async def test_wholesale_retail_sandbox_previews_without_saving(
    admin_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_extract_caption(self, caption: str):
        assert caption == "Black tote, size M, ₦10,000"
        return CaptionExtraction(
            wholesale_price=Decimal("10000"),
            category="bag",
            sizes=["M"],
            colors=["black"],
            product_name="Tote",
        )

    monkeypatch.setattr(LLMAgent, "extract_caption", fake_extract_caption)
    await admin_client.put(
        "/api/admin/settings",
        json={
            "wholesale_group_jid": None,
            "retail_group_jid": None,
            "mum_admin_jid": None,
            "wholesaler_jids": [],
            "markup_rule": "2500",
            "min_profit_rule": "500",
            "payment_instructions": "",
            "automation_enabled": True,
        },
    )

    response = await admin_client.post(
        "/api/admin/sandbox/wholesale-retail",
        json={"caption": "Black tote, size M, ₦10,000"},
    )

    assert response.status_code == 200
    result = response.json()
    assert Decimal(result["listed_price"]) == Decimal("12500")
    assert Decimal(result["floor_price"]) == Decimal("10500")
    assert f"Price: ₦{result['listed_price']}" in result["retail_caption"]
    assert "Wholesale price" not in result["retail_caption"]
    assert "10500" not in result["retail_caption"]
    assert "✨" not in result["retail_caption"]
    assert result["retail_caption"] == (
        f"Tote\nPrice: ₦{result['listed_price']}\nColours: black\nSizes: M"
    )
    async with app.state.test_sessions() as session:
        assert await session.scalar(select(Product.id)) is None


@pytest.mark.asyncio
async def test_wholesale_sandbox_previews_variant_without_inheriting_price(
    admin_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    extracted_captions = []

    async def fake_extract_caption(self, caption: str):
        extracted_captions.append(caption)
        if caption == "hello, more black":
            return CaptionExtraction(wholesale_price=None, category="shoe")
        return CaptionExtraction(
            wholesale_price=Decimal("15000"),
            category="shoe",
        )

    comparisons = []

    async def fake_compare_product_images(
        self,
        previous_image: bytes,
        previous_mime_type: str,
        candidate_image: bytes,
        candidate_mime_type: str,
    ):
        comparisons.append(
            (previous_image, previous_mime_type, candidate_image, candidate_mime_type)
        )
        return ProductImageComparison(
            decision=ProductImageMatch.SAME_PRODUCT,
            confidence=0.96,
            reason="The shoe construction matches.",
        )

    monkeypatch.setattr(LLMAgent, "extract_caption", fake_extract_caption)
    monkeypatch.setattr(
        LLMAgent,
        "compare_product_images",
        fake_compare_product_images,
    )
    response = await admin_client.post(
        "/api/admin/sandbox/wholesale-retail",
        json={
            "caption": "hello, more black",
            "previous_priced_caption": "Original shoe price ₦15,000",
            "previous_image_base64": "cHJldmlvdXM=",
            "previous_image_mime_type": "image/jpeg",
            "image_base64": "bmV3",
            "image_mime_type": "image/png",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["accepted"] is True
    assert result["variant_followup"] is True
    assert result["price_added"] is False
    assert result["retail_caption"] == "hello, more black"
    assert extracted_captions == ["hello, more black"]
    assert comparisons == [
        (b"previous", "image/jpeg", b"new", "image/png")
    ]


@pytest.mark.asyncio
async def test_wholesale_preview_returns_gateway_error_when_ai_fails(
    admin_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    async def failed_extract_caption(self, caption: str):
        raise LLMProviderError("Groq could not return a valid response")

    monkeypatch.setattr(LLMAgent, "extract_caption", failed_extract_caption)
    response = await admin_client.post(
        "/api/admin/sandbox/wholesale-retail",
        json={"caption": "Tote, ₦12,000"},
    )

    assert response.status_code == 502
    assert "Please retry" in response.json()["detail"]


@pytest.mark.asyncio
async def test_wholesale_handler_skips_caption_without_single_unit_price(
    admin_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_extract_caption(self, caption: str):
        return CaptionExtraction(
            wholesale_price=None,
            category="shoe",
            sizes=["37", "38"],
        )

    monkeypatch.setattr(LLMAgent, "extract_caption", fake_extract_caption)
    async with app.state.test_sessions() as session:
        session.add(
            BusinessSettings(
                id=1,
                automation_enabled=True,
                wholesale_group_jid="wholesale@g.us",
                markup_rule=Decimal("2500"),
                min_profit_rule=Decimal("500"),
            )
        )
        await session.commit()
        result = await Orchestrator(session).handle_wholesale(
            MediaPayload(
                message_id="bulk-only",
                from_jid="seller@s.whatsapp.net",
                chat_jid="wholesale@g.us",
                caption="#2,700 carton by 36\n#2,800 MOQ 12\n#2,900 MOQ 3",
                image_base64="aW1hZ2U=",
            )
        )

        assert result["accepted"] is False
        assert "single-unit" in result["reason"]
        assert await session.scalar(select(Product.id)) is None


@pytest.mark.asyncio
async def test_wholesale_handler_accepts_posts_from_any_selected_group(
    admin_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    async def fake_extract_caption(self, caption: str):
        return CaptionExtraction(wholesale_price=Decimal("10000"), category="bag")

    scheduled_posts = []

    async def capture_post(self, product_id, caption):
        scheduled_posts.append((product_id, caption))

    monkeypatch.setattr(LLMAgent, "extract_caption", fake_extract_caption)
    monkeypatch.setattr(Orchestrator, "_jittered_repost", capture_post)

    async with app.state.test_sessions() as session:
        session.add(
            BusinessSettings(
                id=1,
                automation_enabled=True,
                wholesale_group_jid="first@g.us",
                wholesale_group_jids=["first@g.us", "second@g.us"],
                retail_group_jid="retail@g.us",
                markup_rule=Decimal("2500"),
                min_profit_rule=Decimal("500"),
            )
        )
        await session.commit()

        result = await Orchestrator(session).handle_wholesale(
            MediaPayload(
                message_id="second-group-post",
                from_jid="seller@s.whatsapp.net",
                chat_jid="second@g.us",
                caption="Tote, ₦10,000",
                image_base64="aW1hZ2U=",
            )
        )
        await asyncio.sleep(0)

    assert result["accepted"] is True
    assert len(scheduled_posts) == 1


@pytest.mark.asyncio
async def test_wholesale_handler_ignores_retail_destination(
    admin_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    async def unexpected_extraction(self, caption: str):
        raise AssertionError("Retail posts must not be sent back through ingestion")

    monkeypatch.setattr(LLMAgent, "extract_caption", unexpected_extraction)
    async with app.state.test_sessions() as session:
        session.add(
            BusinessSettings(
                id=1,
                automation_enabled=True,
                wholesale_group_jids=["retail@g.us"],
                retail_group_jid="retail@g.us",
                markup_rule=Decimal("2500"),
                min_profit_rule=Decimal("500"),
            )
        )
        await session.commit()

        result = await Orchestrator(session).handle_wholesale(
            MediaPayload(
                message_id="retail-image",
                from_jid="seller@s.whatsapp.net",
                chat_jid="retail@g.us",
                caption="A retail image",
                image_base64="aW1hZ2U=",
            )
        )

    assert result == {
        "accepted": False,
        "reason": "unexpected_wholesale_group",
    }


@pytest.mark.asyncio
async def test_unvalidated_price_is_not_reposted_as_a_variant(
    admin_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    async def fake_extract_caption(self, caption: str):
        return CaptionExtraction(wholesale_price=None, category="shoe")

    async def unexpected_image_comparison(*args, **kwargs):
        raise AssertionError("A caption with a price must not enter variant matching")

    monkeypatch.setattr(LLMAgent, "extract_caption", fake_extract_caption)
    monkeypatch.setattr(
        LLMAgent,
        "compare_product_images",
        unexpected_image_comparison,
    )
    async with app.state.test_sessions() as session:
        original_image = tmp_path / "original.jpg"
        original_image.write_bytes(b"previous image")
        session.add_all(
            [
                BusinessSettings(
                    id=1,
                    automation_enabled=True,
                    wholesale_group_jid="wholesale@g.us",
                    markup_rule=Decimal("2500"),
                    min_profit_rule=Decimal("500"),
                ),
                Product(
                    wholesale_msg_id="priced-original",
                    wholesaler_jid="seller@s.whatsapp.net",
                    image_path=str(original_image),
                    raw_caption="Shoe, ₦15,000",
                    category="shoe",
                    available_metadata={},
                    wholesale_price=Decimal("15000"),
                    listed_price=Decimal("20000"),
                    floor_price=Decimal("15500"),
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                ),
            ]
        )
        await session.commit()

        result = await Orchestrator(session).handle_wholesale(
            MediaPayload(
                message_id="unvalidated-price",
                from_jid="seller@s.whatsapp.net",
                chat_jid="wholesale@g.us",
                caption="#15,000\nmore black",
                image_base64="aW1hZ2U=",
            )
        )

    assert result["accepted"] is False
    assert "single-unit" in result["reason"]


@pytest.mark.asyncio
async def test_wholesale_followup_variant_reposts_original_caption_without_price(
    admin_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    async def fake_extract_caption(self, caption: str):
        return CaptionExtraction(wholesale_price=None, category="shoe")

    comparisons = []

    async def fake_compare_product_images(
        self,
        previous_image: bytes,
        previous_mime_type: str,
        candidate_image: bytes,
        candidate_mime_type: str,
    ):
        comparisons.append((previous_image, candidate_image))
        return ProductImageComparison(
            decision=ProductImageMatch.SAME_PRODUCT,
            confidence=0.96,
            reason="Sole and stitching match.",
        )

    scheduled_posts = []

    async def capture_variant_post(self, image_path: str, original_caption: str):
        scheduled_posts.append((image_path, original_caption))

    monkeypatch.setattr(LLMAgent, "extract_caption", fake_extract_caption)
    monkeypatch.setattr(
        LLMAgent,
        "compare_product_images",
        fake_compare_product_images,
    )
    monkeypatch.setattr(
        Orchestrator,
        "_save_wholesale_image",
        staticmethod(lambda payload, image_bytes=None: tmp_path / "variant.jpg"),
    )
    monkeypatch.setattr(
        Orchestrator,
        "_jittered_repost_variant",
        capture_variant_post,
    )

    async with app.state.test_sessions() as session:
        original_image = tmp_path / "original.jpg"
        original_image.write_bytes(b"previous image")
        session.add_all(
            [
                BusinessSettings(
                    id=1,
                    automation_enabled=True,
                    wholesale_group_jid="wholesale@g.us",
                    retail_group_jid="retail@g.us",
                ),
                Product(
                    wholesale_msg_id="priced-original",
                    wholesaler_jid="seller@s.whatsapp.net",
                    image_path=str(original_image),
                    raw_caption="Shoe, ₦15,000",
                    category="shoe",
                    available_metadata={},
                    wholesale_price=Decimal("15000"),
                    listed_price=Decimal("20000"),
                    floor_price=Decimal("15500"),
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                ),
            ]
        )
        await session.commit()

        result = await Orchestrator(session).handle_wholesale(
            MediaPayload(
                message_id="black-variant",
                from_jid="seller@s.whatsapp.net",
                chat_jid="wholesale@g.us",
                caption="hello, more black",
                image_base64="aW1hZ2U=",
            )
        )
        await asyncio.sleep(0)

        assert result == {
            "accepted": True,
            "variant_followup": True,
            "price_added": False,
        }
        assert scheduled_posts == [
            (str(tmp_path / "variant.jpg"), "hello, more black")
        ]
        assert comparisons == [(b"previous image", b"image")]
        assert (
            await session.scalar(
                select(Product.id).where(
                    Product.wholesale_msg_id == "black-variant"
                )
            )
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "confidence"),
    [
        (ProductImageMatch.DIFFERENT_PRODUCT, 0.99),
        (ProductImageMatch.UNCERTAIN, 0.99),
        (ProductImageMatch.SAME_PRODUCT, 0.89),
    ],
)
async def test_wholesale_followup_requires_a_confident_same_product_match(
    admin_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    decision: ProductImageMatch,
    confidence: float,
):
    async def fake_extract_caption(self, caption: str):
        return CaptionExtraction(wholesale_price=None, category="shoe")

    async def fake_compare_product_images(self, *args, **kwargs):
        return ProductImageComparison(
            decision=decision,
            confidence=confidence,
            reason="Image comparison result.",
        )

    scheduled_posts = []

    async def capture_variant_post(self, image_path: str, original_caption: str):
        scheduled_posts.append((image_path, original_caption))

    monkeypatch.setattr(LLMAgent, "extract_caption", fake_extract_caption)
    monkeypatch.setattr(
        LLMAgent,
        "compare_product_images",
        fake_compare_product_images,
    )
    monkeypatch.setattr(
        Orchestrator,
        "_jittered_repost_variant",
        capture_variant_post,
    )
    async with app.state.test_sessions() as session:
        original_image = tmp_path / "original.jpg"
        original_image.write_bytes(b"previous image")
        session.add_all(
            [
                BusinessSettings(
                    id=1,
                    automation_enabled=True,
                    wholesale_group_jid="wholesale@g.us",
                    retail_group_jid="retail@g.us",
                ),
                Product(
                    wholesale_msg_id="priced-original",
                    wholesaler_jid="seller@s.whatsapp.net",
                    image_path=str(original_image),
                    raw_caption="Shoe, ₦15,000",
                    category="shoe",
                    available_metadata={},
                    wholesale_price=Decimal("15000"),
                    listed_price=Decimal("20000"),
                    floor_price=Decimal("15500"),
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                ),
            ]
        )
        await session.commit()

        result = await Orchestrator(session).handle_wholesale(
            MediaPayload(
                message_id="uncertain-variant",
                from_jid="seller@s.whatsapp.net",
                chat_jid="wholesale@g.us",
                caption="more black",
                image_base64="aW1hZ2U=",
            )
        )
        await asyncio.sleep(0)

    assert result["accepted"] is False
    assert scheduled_posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("previous_sender", "minutes_ago"),
    [
        ("someone-else@s.whatsapp.net", 1),
        ("seller@s.whatsapp.net", 11),
    ],
)
async def test_wholesale_followup_without_recent_same_sender_is_not_inherited(
    admin_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    previous_sender: str,
    minutes_ago: int,
):
    async def fake_extract_caption(self, caption: str):
        return CaptionExtraction(wholesale_price=None, category="shoe")

    monkeypatch.setattr(LLMAgent, "extract_caption", fake_extract_caption)
    async with app.state.test_sessions() as session:
        session.add_all(
            [
                BusinessSettings(
                    id=1,
                    automation_enabled=True,
                    wholesale_group_jid="wholesale@g.us",
                    retail_group_jid="retail@g.us",
                ),
                Product(
                    wholesale_msg_id="priced-original",
                    wholesaler_jid=previous_sender,
                    image_path="media/original.jpg",
                    raw_caption="Shoe, ₦15,000",
                    category="shoe",
                    available_metadata={},
                    wholesale_price=Decimal("15000"),
                    listed_price=Decimal("20000"),
                    floor_price=Decimal("15500"),
                    created_at=datetime.now(timezone.utc)
                    - timedelta(minutes=minutes_ago),
                ),
            ]
        )
        await session.commit()
        result = await Orchestrator(session).handle_wholesale(
            MediaPayload(
                message_id="unmatched-variant",
                from_jid="seller@s.whatsapp.net",
                chat_jid="wholesale@g.us",
                caption="more black",
                image_base64="aW1hZ2U=",
            )
        )

    assert result["accepted"] is False
    assert "single-unit" in result["reason"]


@pytest.mark.asyncio
async def test_dashboard_settings_persist_and_pause_automation(
    admin_client: httpx.AsyncClient,
):
    response = await admin_client.get("/api/admin/settings")
    assert response.status_code == 200
    assert Decimal(response.json()["markup_rule"]) == Decimal("2500")

    response = await admin_client.put(
        "/api/admin/settings",
        json={
            "wholesale_group_jid": "wholesale@g.us",
            "retail_group_jid": "retail@g.us",
            "mum_admin_jid": "admin@s.whatsapp.net",
            "wholesaler_jids": ["seller@s.whatsapp.net"],
            "markup_rule": "5000",
            "min_profit_rule": "1000",
            "payment_instructions": "Bank transfer details",
            "automation_enabled": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["wholesale_group_jid"] == "wholesale@g.us"
    assert Decimal(response.json()["markup_rule"]) == Decimal("5000")

    response = await admin_client.post("/api/admin/automation/pause")
    assert response.status_code == 200
    assert response.json()["automation_enabled"] is False

    response = await admin_client.get("/api/admin/settings")
    assert response.json()["automation_enabled"] is False


@pytest.mark.asyncio
async def test_posting_settings_update_preserves_disabled_workflow_settings(
    admin_client: httpx.AsyncClient,
):
    await admin_client.put(
        "/api/admin/settings",
        json={
            "mum_admin_jid": "admin@s.whatsapp.net",
            "wholesaler_jids": ["seller@s.whatsapp.net"],
            "markup_rule": "2500",
            "min_profit_rule": "500",
            "payment_instructions": "Existing payment info",
            "automation_enabled": True,
        },
    )

    response = await admin_client.put(
        "/api/admin/settings",
        json={
            "wholesale_group_jid": "wholesale@g.us",
            "retail_group_jid": "retail@g.us",
            "markup_rule": "3000",
            "automation_enabled": True,
        },
    )

    assert response.status_code == 200
    settings = response.json()
    assert settings["mum_admin_jid"] == "admin@s.whatsapp.net"
    assert settings["wholesaler_jids"] == ["seller@s.whatsapp.net"]
    assert settings["min_profit_rule"] == "500.00"
    assert settings["payment_instructions"] == "Existing payment info"


@pytest.mark.asyncio
async def test_dashboard_rejects_floor_above_markup(
    admin_client: httpx.AsyncClient,
):
    response = await admin_client.put(
        "/api/admin/settings",
        json={
            "wholesale_group_jid": None,
            "retail_group_jid": None,
            "mum_admin_jid": None,
            "wholesaler_jids": [],
            "markup_rule": "500",
            "min_profit_rule": "600",
            "payment_instructions": "",
            "automation_enabled": True,
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_admin_can_update_inquiry_and_handoff(
    admin_client: httpx.AsyncClient,
):
    async with app.state.test_sessions() as session:
        conversation = CustomerConversation(
            customer_jid="customer@s.whatsapp.net",
            customer_name="Customer",
        )
        product = Product(
            wholesale_msg_id="source-message",
            wholesaler_jid="seller@s.whatsapp.net",
            image_path="media/item.jpg",
            raw_caption="Bag",
            category="bag",
            wholesale_price=100,
            listed_price=150,
            floor_price=120,
        )
        session.add_all([conversation, product])
        await session.flush()
        inquiry = OrderInquiry(
            customer_jid=conversation.customer_jid,
            product_id=product.id,
            status=OrderInquiryStatus.NEGOTIATING,
        )
        session.add(inquiry)
        await session.commit()
        inquiry_id = str(inquiry.id)

    response = await admin_client.patch(
        f"/api/admin/inquiries/{inquiry_id}",
        json={"status": "HANDOFF_TO_MUM"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "HANDOFF_TO_MUM"

    async with app.state.test_sessions() as session:
        conversation = await session.scalar(
            select(CustomerConversation).where(
                CustomerConversation.customer_jid == "customer@s.whatsapp.net"
            )
        )
        assert conversation.bot_muted is True


@pytest.mark.asyncio
async def test_dashboard_requires_custom_request_header(
    admin_client: httpx.AsyncClient,
):
    response = await admin_client.get(
        "/api/admin/settings",
        headers={"X-Dashboard-Request": ""},
    )
    assert response.status_code == 403
