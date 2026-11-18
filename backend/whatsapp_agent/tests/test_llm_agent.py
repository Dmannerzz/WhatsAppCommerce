import json
from types import SimpleNamespace

import pytest

from whatsapp_agent.services.llm_agent import (
    LLMAgent,
    LLMOutputError,
    NegotiationIntent,
    StockAvailability,
)


class FakeClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.content)
                )
            ]
        )


@pytest.mark.asyncio
async def test_caption_extraction():
    agent = LLMAgent(
        FakeClient(
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
    )
    result = await agent.extract_caption("Black tote, size M, 12000")
    assert result.category == "bag"
    assert str(result.wholesale_price) == "12000"


@pytest.mark.asyncio
async def test_negotiation_rejects_floor_disclosure():
    agent = LLMAgent(
        FakeClient(
            json.dumps(
                {
                    "reply_text": "My lowest is 12500.",
                    "extracted_size": "M",
                    "extracted_color": "black",
                    "customer_offered_price": None,
                    "intent": "NEGOTIATING",
                    "handoff_reason": None,
                }
            )
        )
    )
    with pytest.raises(LLMOutputError):
        await agent.negotiate(
            customer_name="Customer",
            customer_message="Lowest?",
            history=[],
            product={
                "listed_price": "15000",
                "floor_price": "12500",
                "sizes": ["M"],
                "colors": ["black"],
            },
        )


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
