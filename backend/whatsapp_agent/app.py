from contextlib import asynccontextmanager

from fastapi import FastAPI

from whatsapp_agent.db import engine
from whatsapp_agent.models import Base
from whatsapp_agent.routes.webhooks import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(
    title="WhatsApp Retail & Wholesale Arbitrage Agent",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
