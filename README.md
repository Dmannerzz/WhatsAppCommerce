# WhatsApp Retail & Wholesale Arbitrage Agent

Two-service monorepo:

- `backend/`: async FastAPI, SQLAlchemy, PostgreSQL, and LLM orchestration.
- `whatsapp-bridge/`: Baileys-based WhatsApp transport.

## Local development

1. Copy `.env.example` to `.env` and replace placeholder values.
2. Start PostgreSQL with `docker compose up -d postgres`.
3. Install backend dependencies with `cd backend && uv sync`.
4. Install bridge dependencies with `cd whatsapp-bridge && npm install`.

The existing AviX project is unrelated and is not included in this repository.
