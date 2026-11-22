# Backend

FastAPI backend for the WhatsApp retail and wholesale arbitrage workflows.

For quick local development, set `DATABASE_URL=sqlite+aiosqlite:///./whatsapp_agent.db`
in the repository-root `.env`. PostgreSQL is optional locally; to use it, set a
PostgreSQL async URL and run `docker compose up -d postgres` from the repository
root. Install dependencies:

```powershell
uv sync
```

Database migrations and the FastAPI application entry point are added in later
implementation stages.

Run the backend:

```powershell
uv run python main.py
```

The protected admin dashboard is available at `/api/admin`. Set
`ADMIN_USERNAME`, a unique `ADMIN_PASSWORD` (at least 16 characters),
`BRIDGE_API_SECRET`, and `BRIDGE_WEBHOOK_SECRET` in the repository `.env`
before starting the services. The same bridge API secret must be set for the
backend and bridge.

For Vercel deployment, see the root README. Vercel uses
`BRIDGE_TRANSPORT=database` so the FastAPI service and Baileys daemon communicate
through PostgreSQL; local development keeps the default HTTP bridge transport.

Run tests:

```powershell
uv run pytest -q
```
