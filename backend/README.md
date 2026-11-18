# Backend

FastAPI backend for the WhatsApp retail and wholesale arbitrage workflows.

Start PostgreSQL from the repository root, then install dependencies:

```powershell
uv sync
```

Database migrations and the FastAPI application entry point are added in later
implementation stages.

Run the backend:

```powershell
uv run python main.py
```

Run tests:

```powershell
uv run pytest -q
```
