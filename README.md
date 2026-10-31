# SmeOps

SmeOps is a multi-tenant WhatsApp-based AI customer assistant SaaS for appointment-based and retail businesses, starting in Nigeria.

## Delivery status

**Phase 1 — Core Platform and Business Operations: in progress.**

The repository is being established incrementally. The current foundation includes the API, PostgreSQL persistence, tenant registry, owner authentication, service catalogue, business hours, appointment booking and audit events. It is not production-ready yet.

## Architecture baseline

- Python 3.12 and FastAPI for the API.
- PostgreSQL 16 for durable application state.
- SQLAlchemy 2.x and Alembic for persistence and schema migrations.
- Deterministic backend services own bookings, orders, authorization and state transitions.
- WhatsApp Cloud API integration will be isolated behind an adapter.
- Background work is persisted in PostgreSQL and claimed with `FOR UPDATE SKIP LOCKED`; webhook handlers must acknowledge quickly.
- Tenant identity is derived from signed server-side authentication context, never trusted from client-supplied tenant IDs.
- The LLM will not receive direct database access. It may only request explicitly authorized application tools in a later phase.

## Local development

1. Copy `.env.example` to `.env` and set a unique `APP_SECRET_KEY` (at least 32 characters).
2. Start PostgreSQL: `docker compose up -d db`.
3. Install: `python -m pip install -e '.[dev]'`.
4. Run migrations: `alembic upgrade head`.
5. Start API: `uvicorn app.main:app --reload`.
6. Run tests: `pytest`.

Endpoints:
- `GET /health/live`: process liveness only.
- `GET /health/ready`: checks PostgreSQL connectivity.
- `POST /auth/register`: creates a business tenant and its initial owner.
- `POST /auth/login`: authenticates an owner using business slug, email and password.
- `GET /auth/me`: returns the authenticated account and tenant identifiers.
- `POST /services`, `GET /services`: manage and list a tenant's service catalogue.
- `PUT /business-hours`, `GET /business-hours`: replace and retrieve the tenant's weekly opening schedule.
- `POST /appointments`, `GET /appointments`: create and list tenant appointments.
- `POST /appointments/{id}/cancel`: cancel a confirmed appointment.
- `POST /products`, `GET /products`: manage and list a tenant's retail catalogue.
- `POST /orders`, `GET /orders`: create idempotent order drafts and list tenant orders.
- `POST /orders/{id}/approve`: owner/manager approval and atomic stock deduction.
- `POST /orders/{id}/cancel`: cancel an order awaiting approval.
- `POST /orders/{id}/fulfill`: mark an approved order fulfilled.

Prices are represented as integer minor currency units (kobo) and currency is currently fixed to NGN. Business hours currently support one opening interval per weekday. Appointment creation requires a client-generated idempotency UUID; replays with the same request return the original booking, while key reuse with a different payload is rejected. Appointment timestamps must include a timezone offset and are stored in UTC. Retail orders are drafts pending owner/manager approval; stock is deducted only at approval. Payment, delivery and post-approval refunds are outside this increment.

## Security status and limitations

- Passwords are hashed using Argon2.
- Access tokens are signed with HS256 and contain only user and tenant identifiers plus standard claims.
- Authenticated requests reload the user and tenant from PostgreSQL and reject disabled accounts or suspended tenants.
- Tenant-scoped composite foreign keys prevent appointments from referencing another tenant's services, customers or users.
- Appointment writes lock the tenant row before checking overlaps, serializing booking attempts for one business while allowing different businesses to book concurrently.
- Service, business-hours, appointment, product and order changes write audit events in the same database transaction.
- The generic job queue supports idempotent enqueueing, bounded retries, expiring worker leases, and dead-letter state. Job handlers must be idempotent because a worker can lose its lease after an external side effect.
- Registration and login rate limiting, email verification, password reset, refresh-token rotation, CSRF protections for cookie-based sessions, and full audit-log access/retention controls are not yet implemented.
- Appointment rescheduling, customer self-service and resource/staff-specific scheduling are not yet implemented.
- WhatsApp event ingestion and production job handlers are not yet connected to the queue.
- Do not expose this API publicly as a production service until those controls and the remaining Phase 1 exit criteria are complete.
- The local default secret is rejected when `APP_ENV=production`; production must inject a unique secret through a secret manager.

## Engineering rules

- No secrets committed to the repository.
- All tenant-owned queries must be tenant-scoped and tested for cross-tenant denial.
- Business operations must be transactional, idempotent where retried, and auditable.
- Time values are stored in UTC; business timezone is explicit per tenant.
- AI-generated text is never authoritative for prices, availability, booking state or order state.
- CI must pass before a change is merged.

## Phase 1 exit criteria

- Tenant isolation and role authorization are enforced server-side.
- Appointment and retail workflows are deterministic and transaction-safe.
- WhatsApp webhook verification, signature validation and idempotency are tested.
- Background jobs support bounded retries and dead-letter handling.
- Audit events cover security-sensitive and business-critical state transitions.
- Unit, integration and end-to-end tests pass against PostgreSQL.
- Operational limits, backup/restore and deployment smoke tests are evidenced.
