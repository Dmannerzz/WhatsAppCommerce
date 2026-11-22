# WhatsApp Retail & Wholesale Arbitrage Agent

Two-service monorepo:

- `backend/`: async FastAPI, SQLAlchemy, PostgreSQL, and LLM orchestration.
- `whatsapp-bridge/`: Baileys-based WhatsApp transport.

## Local development

1. Copy `.env.example` to `.env` and replace placeholder values. Generate
   independent secrets with
   `python -c "import secrets; print(secrets.token_urlsafe(32))"` for
   `ADMIN_PASSWORD`, `BRIDGE_API_SECRET`, and `BRIDGE_WEBHOOK_SECRET`.
2. For the quickest local setup, use the SQLite `DATABASE_URL` shown in
   `.env.example`. No database server or Docker is required. To use PostgreSQL
   instead, set the PostgreSQL `DATABASE_URL` and run
   `docker compose up -d postgres`.
3. Install backend dependencies with `cd backend && uv sync`.
4. Install bridge dependencies with `cd whatsapp-bridge && npm install`.
5. Start the backend with `cd backend && uv run python main.py`, then the
   bridge with `cd whatsapp-bridge && npm run dev`.
6. Open [http://localhost:8000/api/admin](http://localhost:8000/api/admin) and
   sign in with `ADMIN_USERNAME` and `ADMIN_PASSWORD`. The browser will show
   the Baileys pairing QR when the bridge is waiting for login.
7. After linking WhatsApp, choose **Load WhatsApp groups** in Business
   settings. Select one or more wholesale groups and one retail destination
   from the returned lists. Existing installations automatically keep their
   previously configured wholesale group as a selected source.
   The linked account's JID is shown in Connection; **Use linked** copies it
   into the admin WhatsApp field. Supplier phone JIDs can be entered in
   international format as digits followed by `@s.whatsapp.net`.
8. The admin dashboard guides first-time setup: connect WhatsApp, choose the
   wholesale and retail groups, review the markup, and enable automatic posting.
   Use **Test a caption** to open the separate posting sandbox. It previews
   wholesale-caption extraction, calculated prices, and the retail caption
   without sending WhatsApp messages or saving a product. A Groq JSON-format
   failure is retried once; persistent AI service errors are reported as
   gateway errors, not server 500s. To test a variant follow-up like “more
   black,” provide the earlier priced caption and upload both product images;
   the preview assumes the same sender and a 10-minute window but never carries
   over that price. Both images are sent to Groq for comparison. `VISION_MODEL`
   selects the vision model (default `qwen/qwen3.8-27b`).

The admin dashboard requires a unique password of at least 16 characters.
Use HTTPS and a private network or VPN when exposing either service outside
the local machine. The bridge API and webhook secrets must be different.
SQLite is intended for local development; use PostgreSQL for multi-user or
production deployments.

## Deploying to Vercel

The root `vercel.json` deploys the FastAPI dashboard/API as a Vercel Service
and the Baileys bridge as a 1 GiB Vercel Daemon. In Vercel mode, the bridge
polls a PostgreSQL command queue instead of exposing an inbound API. WhatsApp
credentials are encrypted in PostgreSQL; product images are stored in Vercel
Blob. The retail images use public Blob URLs because the bridge must download
them to send through WhatsApp.

1. Import this GitHub repository as a Vercel project. Keep the project root at
   the repository root so Vercel can read `vercel.json` and build both services.
2. Add a PostgreSQL database through the Vercel Marketplace (Neon is supported)
   and a **public** Vercel Blob store connected to this project. Use the
   database's pooled connection string for `DATABASE_URL`; connect Blob with
   Vercel OIDC or add its `BLOB_READ_WRITE_TOKEN`.
3. Add these project environment variables for **Production**:
   - `BRIDGE_TRANSPORT=database`
   - `BRIDGE_API_PORT=3001` for the daemon liveness check
   - `BACKEND_WEBHOOK_URL=https://<your-project-domain>/api/webhooks`
   - `ADMIN_USERNAME=mum` and a unique `ADMIN_PASSWORD` of at least 16
     characters
   - `GROQ_API_KEY`
   - independent random values of at least 32 characters for
     `BRIDGE_API_SECRET` and `BRIDGE_WEBHOOK_SECRET`
   - `WHATSAPP_AUTH_ENCRYPTION_KEY`, a base64-encoded random 32-byte key
   - `DATABASE_URL` and Blob credentials (`BLOB_STORE_ID` with Vercel OIDC, or
     `BLOB_READ_WRITE_TOKEN`) from the storage integrations
4. Deploy to Production. Open
   `https://<your-project-domain>/api/admin` on the phone, sign in, scan the
   pairing QR, select the wholesale sources and retail group, and finish setup.

Generate the auth-encryption key locally with
`python -c "import secrets,base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"`.
Set it directly in Vercel and keep it backed up securely: changing or losing it
prevents the bridge from restoring its saved WhatsApp session. Do not put
secrets in Git or share them in chat.

Vercel Daemons have no persistent local disk or public inbound traffic; those
constraints are handled by encrypted database-backed auth, Blob media, and the
database command queue. The Vercel Hobby daemon quota is 420 GB-hours and 5
active CPU-hours per month. A 1 GiB daemon running continuously reaches the
memory quota in about 17.5 days, at which point Vercel stops it until the next
month (UTC). WhatsApp posting and group refresh will pause while it is stopped;
queued posts are retained and rate-limited when processing resumes. Storage
integrations have their own limits, so review their current free-tier quotas
before enabling automation.

## Alternative deployment: Render

The root `render.yaml` Blueprint creates a public FastAPI service, a private
Baileys bridge, PostgreSQL, and persistent disks for WhatsApp login credentials
and product images. These are paid Render resources; review the current prices
and disk/database charges in Render before creating the Blueprint. Keep the
services in one region and at one instance each so the persistent disks remain
available.

1. Push this repository to GitHub and create a Blueprint in Render using
   `render.yaml`. Review the resources and costs before confirming.
2. When prompted, provide `ADMIN_PASSWORD` (at least 16 characters),
   `GROQ_API_KEY`, `BRIDGE_API_SECRET`, and `BRIDGE_WEBHOOK_SECRET`. Render
   prompts for the two bridge secrets on both services; enter the exact same
   value for each secret on the backend and bridge. Do not put secret values in
   Git or share them in chat.
3. Wait for the backend and bridge to deploy. Open the backend's public
   `onrender.com` URL at `/api/admin` on the phone and sign in with username
   `mum` and the admin password you supplied.
4. Pair WhatsApp from the dashboard, load the available groups, select the
   wholesale sources and retail destination, then finish the setup steps.

The Blueprint connects PostgreSQL using Render's internal network. WhatsApp
credentials and product media are stored on persistent disks. Keep the bridge
at one instance; do not remove or recreate its disk after pairing unless you
intend to link WhatsApp again. The backend URL is public and protected by the
dashboard login; the bridge remains private.

The bridge processes product image posts from the configured wholesale group
and reposts them to the retail group. Tiered prices are checked for a
single-unit price; listings with only MOQ/carton prices are skipped. Customer
and wholesaler messages are not automatically processed or answered. When a
wholesaler lists sizes per colour, the retail caption keeps each size grouped
with its colour. A same-sender image variant within ten minutes of a priced
item is reposted unchanged only when Groq confirms a high-confidence match.
Captions containing an unvalidated price are never forwarded as raw variant
captions. Product images used for this comparison are sent to Groq.

The existing AviX project is unrelated and is not included in this repository.
