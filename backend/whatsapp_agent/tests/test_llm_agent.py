import json
from types import SimpleNamespace

import httpx
import pytest
from groq import BadRequestError

from whatsapp_agent.services.llm_agent import (
    LLMAgent,
    LLMProviderError,
    NegotiationIntent,
    ProductImageComparison,
    ProductImageMatch,
    StockAvailability,
)


class FakeClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.last_request = None
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.last_request = kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.content)
                )
            ]
        )


class RetryFakeClient(FakeClient):
    def __init__(self, content: str, failures: int) -> None:
        super().__init__(content)
        self.failures = failures
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            response = httpx.Response(
                400,
                request=httpx.Request("POST", "https://api.groq.com"),
            )
            raise BadRequestError(
                "Failed to validate JSON",
                response=response,
                body={
                    "error": {
                        "code": "json_validate_failed",
                        "failed_generation": "",
                    }
                },
            )
        return await super().create(**kwargs)


@pytest.mark.asyncio
async def test_caption_extraction():
    client = FakeClient(
        json.dumps(
            {
                "wholesale_price": "12000",
                "category": "bag",
                "sizes": ["M"],
                "colors": ["black"],
                "product_name": "Tote",
            }
        )
    )
    agent = LLMAgent(client)
    result = await agent.extract_caption("Black tote, size M, 12000")
    assert result.category == "bag"
    assert str(result.wholesale_price) == "12000"
    assert "json" in client.last_request["messages"][0]["content"].lower()


@pytest.mark.asyncio
async def test_caption_extraction_retries_groq_json_validation_error():
    client = RetryFakeClient(
        json.dumps(
            {
                "wholesale_price": "12000",
                "category": "bag",
                "sizes": [],
                "colors": [],
                "product_name": "Tote",
            }
        ),
        failures=1,
    )

    result = await LLMAgent(client).extract_caption("Tote, ₦12,000")

    assert str(result.wholesale_price) == "12000"
    assert client.calls == 2


@pytest.mark.asyncio
async def test_caption_extraction_raises_provider_error_after_retry_fails():
    client = RetryFakeClient("{}", failures=2)

    with pytest.raises(LLMProviderError):
        await LLMAgent(client).extract_caption("Tote, ₦12,000")

    assert client.calls == 2


@pytest.mark.asyncio
async def test_product_image_comparison_sends_two_images_to_vision_model():
    client = FakeClient(
        json.dumps(
            {
                "decision": "SAME_PRODUCT",
                "confidence": 0.96,
                "reason": "The sole and decorative stitching match.",
            }
        )
    )
    agent = LLMAgent(client)

    result = await agent.compare_product_images(
        b"previous",
        "image/jpeg",
        b"new",
        "image/png",
    )

    assert isinstance(result, ProductImageComparison)
    assert result.decision == ProductImageMatch.SAME_PRODUCT
    assert result.confidence == 0.96
    assert client.last_request["model"] == agent.vision_model
    content = client.last_request["messages"][1]["content"]
    assert content[2]["type"] == "image_url"
    assert content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert content[4]["type"] == "image_url"
    assert content[4]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_caption_extraction_reads_inline_color_and_sizes():
    client = FakeClient(
        json.dumps(
            {
                "wholesale_price": "11500",
                "category": "shoe",
                "sizes": [],
                "colors": [],
                "product_name": "Gold",
            }
        )
    )

    result = await LLMAgent(client).extract_caption(
        "#11,500/3\n#11,700/1\n\nGold:37,38"
    )

    assert str(result.wholesale_price) == "11700"
    assert result.colors == ["Gold"]
    assert result.sizes == ["37", "38"]
    assert result.color_sizes == {"Gold": ["37", "38"]}


@pytest.mark.asyncio
async def test_caption_extraction_keeps_sizes_grouped_by_color():
    client = FakeClient(
        json.dumps(
            {
                "wholesale_price": "14900",
                "category": "shoe",
                "sizes": ["38", "41", "37", "40"],
                "colors": ["Gold", "Black", "Wine"],
                "product_name": None,
            }
        )
    )

    result = await LLMAgent(client).extract_caption(
        "Gold 38 41\nBlack 31 33(2) 34(4) 35(5) 36(3) 37\n"
        "Wine 40\n\nMoq 3 14900\n1 pair 15000"
    )

    assert str(result.wholesale_price) == "15000"
    assert result.color_sizes == {
        "Gold": ["38", "41"],
        "Black": ["31", "33", "34", "35", "36", "37"],
        "Wine": ["40"],
    }
    assert result.colors == ["Gold", "Black", "Wine"]
    assert result.sizes == ["38", "41", "31", "33", "34", "35", "36", "37", "40"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caption", "model_price", "expected_price"),
    [
        (
            "Max selection\n\n37-42\n\n"
            "#2,700 carton by 36\n\n"
            "#2,800 moq 12\n\n"
            "#2,900 moq 3",
            "2900",
            None,
        ),
        (
            "#11,500/3\n#11,700/1\nGold:37,38",
            "11500",
            "11700",
        ),
        (
            "Moq 3 14900\n1 pair 15000",
            "14900",
            "15000",
        ),
        (
            "Tote\n#3,000\n#2,800 MOQ 12",
            "2800",
            "3000",
        ),
    ],
)
async def test_caption_extraction_uses_only_single_unit_price_tiers(
    caption: str,
    model_price: str,
    expected_price: str | None,
):
    client = FakeClient(
        json.dumps(
            {
                "wholesale_price": model_price,
                "category": "shoe",
                "sizes": [],
                "colors": [],
                "product_name": "Selection",
            }
        )
    )
    result = await LLMAgent(client).extract_caption(caption)

    assert (
        str(result.wholesale_price) if result.wholesale_price is not None else None
    ) == expected_price


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "leaked_price",
    ["I can go as low as ₦12,500.", "I can do 10k."],
)
async def test_negotiation_hides_internal_prices_and_sanitizes_reply(leaked_price):
    client = FakeClient(
        json.dumps(
            {
                "reply_text": leaked_price,
                "extracted_size": "M",
                "extracted_color": "black",
                "customer_offered_price": None,
                "intent": "NEGOTIATING",
                "handoff_reason": None,
            }
        )
    )
    agent = LLMAgent(client)
    result = await agent.negotiate(
        customer_name="Customer",
        customer_message="Can you do that?",
        history=[],
        product={
            "listed_price": "15000",
            "floor_price": "12500",
            "wholesale_price": "10000",
            "sizes": ["M"],
            "colors": ["black"],
        },
    )

    assert "₦15000" in result.reply_text
    assert "12500" not in result.reply_text
    assert "10000" not in result.reply_text
    assert "floor" not in result.reply_text.lower()
    llm_payload = json.loads(client.last_request["messages"][1]["content"])
    assert "floor_price" not in llm_payload["product"]
    assert "wholesale_price" not in llm_payload["product"]


@pytest.mark.asyncio
async def test_stock_classifier():
    agent = LLMAgent(
        FakeClient(
            json.dumps(
                {
                    "availability": "UNCLEAR",
                    "reason": "The wholesaler asked for clarification.",
                }
            )
        )
    )
    result = await agent.classify_stock(
        "Which size?",
        product_name="Tote",
        color="black",
        size="M",
    )
    assert result.availability == StockAvailability.UNCLEAR
