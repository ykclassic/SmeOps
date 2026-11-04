# SmeOps

SmeOps is a multi-tenant WhatsApp business assistant for appointment-based and retail businesses, starting with salons in Nigeria.

## Delivery status

The repository now contains a working backend foundation, a deterministic salon booking conversation, an owner dashboard, and a Meta Embedded Signup v4 integration. The work is being delivered through stacked draft pull requests and CI-validated increments.

**This is not yet a production-accepted service.** The Meta integration has mocked CI coverage but has not been verified against the operator's real Meta app or a live business number. Production secrets, managed database, deployed API/worker/frontend, backups and real customer-journey acceptance are still required.

## Current capabilities

- Multi-tenant registration, owner/manager authentication and tenant-scoped records.
- Argon2 password hashing, short-lived signed access tokens and persistent login/registration throttling.
- PostgreSQL persistence, SQLAlchemy 2.x, Alembic migrations and transaction-safe business operations.
- Tenant-owned services, prices, weekly opening hours and appointments.
- WhatsApp webhook signature verification, encrypted event persistence, deduplication and durable background jobs.
- Encrypted customer phone numbers and message bodies, conversation history, opt-out state and 24-hour customer-service-window enforcement.
- Durable outbound message ledger with safe retry behavior and Meta delivery-status reconciliation.
- Deterministic salon booking conversation: service selection, customer name, date, availability options, explicit customer confirmation and appointment creation.
- Optional OpenAI Responses API structured intent classification. The model cannot access the database, choose availability, change prices, or create bookings.
- Human escalation and audited owner/manager resolution.
- Next.js owner dashboard for appointments, services, opening hours, WhatsApp inbox and connection setup.
- Meta Embedded Signup v4 backend and frontend flow, encrypted business token storage, WABA/phone ownership verification, app subscription, phone registration and retryable pending state.
- API security headers, production-only docs disablement, worker graceful shutdown, persistent worker heartbeats and health endpoint.
- Dockerfile, local Docker Compose, Render API/worker blueprint and frontend deployment instructions.

## Architecture

- **Frontend:** Next.js App Router. The browser talks to a same-origin API route handler; backend access tokens are stored in an HttpOnly, SameSite=Strict cookie and are not exposed to browser JavaScript or localStorage.
- **API:** Python 3.12 and FastAPI.
- **Database:** PostgreSQL 16, SQLAlchemy 2.x and Alembic.
- **Worker:** Separate Python process claims persisted jobs using PostgreSQL row locking and SKIP LOCKED.
- **WhatsApp:** Meta Cloud API through a provider adapter and Embedded Signup v4.
- **AI:** Optional OpenAI Responses API adapter for intent classification only. Deterministic application services remain authoritative.

The customer journey is:

`WhatsApp inbound → signed webhook → persisted event → inbound worker → conversation state machine → deterministic availability/booking service → persisted appointment → outbound queue → Meta Cloud API → delivery webhook → owner dashboard`

The model has no database credentials and no direct tool execution. Every booking write requires server-side validation and explicit customer confirmation.

## Local development

Requirements: Docker Compose, Python 3.12+, Node.js 22+.

1. Copy `.env.example` to `.env`.
2. Set a unique local `APP_SECRET_KEY` (at least 32 characters).
3. Generate a Fernet key for `WHATSAPP_TOKEN_ENCRYPTION_KEY` when testing WhatsApp encryption.
4. Start PostgreSQL, API and worker:

   ```bash
   docker compose up --build
   ```

   Compose applies Alembic migrations before starting the API. API: `http://localhost:8000`; health: `/health/live`, `/health/ready`; dashboard is run separately.

5. Start the frontend:

   ```bash
   cd frontend
   cp .env.example .env.local
   npm install
   npm run dev
   ```

   Set `SMEOPS_API_BASE_URL=http://localhost:8000` in `frontend/.env.local`. Dashboard: `http://localhost:3000`.

6. Run backend tests and lint:

   ```bash
   python -m pip install -e ".[dev]"
   alembic upgrade head
   alembic check
   ruff check .
   pytest
   ```

7. Run frontend validation:

   ```bash
   cd frontend
   npm run typecheck
   npm run build
   ```

## Important API routes

- `POST /auth/register`, `POST /auth/login`, `GET /auth/me`
- `GET/POST /services`
- `GET/PUT /business-hours`
- `GET/POST /appointments`, `POST /appointments/{id}/cancel`
- `GET /whatsapp/connection`
- `POST /whatsapp/embedded-signup`, `POST /whatsapp/connection/complete`, `POST /whatsapp/connection/disconnect`
- `GET /whatsapp/conversations`
- `GET /whatsapp/conversations/{id}/messages`
- `POST /whatsapp/conversations/{id}/resolve`
- `POST /whatsapp/messages`
- `GET /health/live` — process liveness.
- `GET /health/ready` — PostgreSQL readiness.
- `GET /health/worker` — latest worker heartbeat; 503 when no fresh worker is running.
- `GET /webhooks/whatsapp`, `POST /webhooks/whatsapp` — Meta webhook verification and ingestion.

## Environment variables

Backend API and worker:

- `APP_ENV` — use `local` for development and `production` for deployment.
- `APP_SECRET_KEY` — unique JWT/HMAC secret, at least 32 characters.
- `DATABASE_URL` — PostgreSQL connection string. `postgres://` and `postgresql://` are normalized to the asyncpg driver.
- `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET` — webhook verification and signature validation.
- `WHATSAPP_TOKEN_ENCRYPTION_KEY` — Fernet key; encrypts business access tokens, customer data and the temporary onboarding PIN (which is erased after successful number registration).
- `WHATSAPP_GRAPH_API_VERSION` — pinned Meta Graph API version, `vNN.N`.
- `META_APP_ID`, `META_APP_SECRET` — Meta application credentials; app secret stays server-side.
- `PLATFORM_ADMIN_KEY` — protects the legacy internal provisioning endpoint.
- `OPENAI_API_KEY`, `OPENAI_MODEL` — optional structured intent classification.

Frontend:

- `SMEOPS_API_BASE_URL` — server-side FastAPI origin; do not prefix with `NEXT_PUBLIC_`.
- `NEXT_PUBLIC_META_APP_ID` — public Meta app ID.
- `NEXT_PUBLIC_META_EMBEDDED_SIGNUP_CONFIG_ID` — Embedded Signup v4 configuration ID.
- `NEXT_PUBLIC_META_GRAPH_API_VERSION` — must match the backend Graph API version.
- `NEXT_PUBLIC_META_SOLUTION_ID` — optional partner solution ID when required by the Meta configuration.

In production, settings validation fails closed if the database is local or required application/WhatsApp/Meta secrets are missing. Never commit real secrets or expose secrets as `NEXT_PUBLIC_*` variables.

## WhatsApp onboarding prerequisites

The Meta app owner must complete Meta Business verification, Tech Provider/Solution Partner requirements, app review/Advanced Access, Embedded Signup v4 configuration, allowed domains, and webhook subscriptions. See [docs/whatsapp-onboarding.md](docs/whatsapp-onboarding.md).

Embedded Signup is not equivalent to production approval. CI mocks Graph API calls; a real test business account is still required to verify authorization, WABA subscription, number registration, webhook delivery and real outbound delivery.

## Deployment

- Backend API and worker: Render blueprint in `render.yaml`.
- Frontend: Vercel or another Next.js-compatible host.
- Deployment, environment setup, smoke tests, monitoring and backup/restore requirements: [docs/production-runbook.md](docs/production-runbook.md).
- Salon workflow and AI safety boundary: [docs/salon-booking-conversation.md](docs/salon-booking-conversation.md).
- Outbound messaging rules: [docs/whatsapp-outbound.md](docs/whatsapp-outbound.md).
- Tenant-scoped inbox APIs: [docs/whatsapp-inbox-api.md](docs/whatsapp-inbox-api.md).

Deploy the API and run migrations before starting the worker. Configure the frontend BFF with the API origin. Do not enable external customer traffic until the live acceptance checklist passes.

## Known limitations and production gates

- The first salon calendar is a single shared resource. Staff calendars, multiple shifts, breaks and resource assignment are not yet implemented.
- WhatsApp booking currently supports service selection, name, date, time-slot selection and explicit confirmation. Customer self-service cancellation/rescheduling, reminders, deposits/payments and multilingual evaluation remain outstanding.
- Approved WhatsApp templates outside the 24-hour service window, template management and self-service re-subscription are not implemented. Free-form messages fail closed outside the service window.
- Meta token expiry is stored, but automatic token refresh/rotation and alerting are not implemented.
- Embedded Signup coexistence mode and multiple WhatsApp numbers per tenant are not implemented.
- The AI intent adapter is optional and English-first; quality evaluation on real customer conversations is outstanding.
- Rate limiting is keyed by normalized account identifier in PostgreSQL. An edge-level IP/WAF rate limit is still required for public deployment.
- Email verification, password reset, MFA, refresh-token rotation, staff invitations and account recovery are not implemented.
- Worker heartbeats and health checks exist, but alert delivery, dashboards for dead-letter queues and queue-age SLOs still need production configuration.
- Automated backup scheduling, point-in-time recovery validation, restore drills, retention/deletion workflows, load tests and independent security review are not evidenced yet.
- No live Meta message delivery, public deployment, backup restore or production acceptance is claimed.
- The manual `Live acceptance tests` GitHub Actions workflow is available for the deployed dashboard/API. It uses GitHub secrets for the test owner account and can verify a completed real WhatsApp booking after the test customer has sent the messages.

## Engineering rules

- No secrets in source control, logs, browser storage or API responses.
- Tenant identity is derived from authenticated server-side context.
- Every tenant-owned query is scoped and cross-tenant access is tested.
- Booking and order transitions are deterministic, transactional, auditable and idempotent.
- Store timestamps in UTC; use the tenant's IANA timezone for business hours and customer display.
- AI-generated content is never authoritative for prices, availability or persisted business state.
- CI must pass before merge. Production deployment requires separate live acceptance evidence.
