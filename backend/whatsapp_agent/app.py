from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text

from whatsapp_agent.db import engine
from whatsapp_agent.models import Base
from whatsapp_agent.db import AsyncSessionFactory
from whatsapp_agent.services.business_settings import get_business_settings
from whatsapp_agent.services.llm_agent import (
    LLMConfigurationError,
    LLMOutputError,
    LLMProviderError,
)
from whatsapp_agent.routes.admin import router as admin_router
from whatsapp_agent.routes.bridge import router as bridge_router
from whatsapp_agent.routes.webhooks import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    import logging

    logger = logging.getLogger(__name__)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            settings_columns = await connection.run_sync(
                lambda sync_connection: {
                    column["name"]
                    for column in inspect(sync_connection).get_columns(
                        "business_settings"
                    )
                }
            )
            if "wholesale_group_jids" not in settings_columns:
                column_type = (
                    "JSONB" if connection.dialect.name == "postgresql" else "JSON"
                )
                await connection.execute(
                    text(
                        "ALTER TABLE business_settings "
                        f"ADD COLUMN wholesale_group_jids {column_type} "
                        "NOT NULL DEFAULT '[]'"
                    )
                )
        async with AsyncSessionFactory() as session:
            await get_business_settings(session)
            await session.commit()
    except Exception:
        logger.exception("Lifespan startup failed — check DATABASE_URL and credentials")
        raise
    yield
    await engine.dispose()


app = FastAPI(
    title="WhatsApp Retail & Wholesale Arbitrage Agent",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(router)
app.include_router(admin_router)
app.include_router(bridge_router)


@app.exception_handler(LLMConfigurationError)
async def llm_configuration_error_handler(
    _request: Request,
    _error: LLMConfigurationError,
) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": (
                "AI caption extraction is not configured. Check GROQ_API_KEY."
            )
        },
    )


@app.exception_handler(LLMProviderError)
@app.exception_handler(LLMOutputError)
async def llm_provider_error_handler(
    _request: Request,
    _error: LLMProviderError | LLMOutputError,
) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={
            "detail": (
                "AI caption extraction failed. Please retry; if it continues, "
                "check the configured Groq model and API key."
            )
        },
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
