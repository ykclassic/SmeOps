# SmeOps

SmeOps is a multi-tenant WhatsApp-based AI customer assistant SaaS for appointment-based and retail businesses, starting in Nigeria.

## Delivery status

**Phase 1 — Core Platform and Business Operations: in progress.**

The current foundation includes PostgreSQL persistence, tenant registration and authentication, service catalogue, business hours, appointment booking, retail orders, inventory approval, audit events, a durable job queue and signed WhatsApp webhook ingestion. It is not production-ready yet.

## Architecture baseline

- Python 3.12 and FastAPI for the API.
- PostgreSQL 16 for durable application state.
- SQLAlchemy 2.x and Alembic for persistence and schema migrations.
- Deterministic backend services own bookings, orders, authorization and state transitions.
- WhatsApp access tokens are encrypted at rest; webhook signatures are verified before parsing or persistence.
- Webhook event payloads are encrypted and deduplicated before being queued for background processing.
- Background work is persisted in PostgreSQL and claimed with PostgreSQL row locking and SKIP LOCKED; webhook handlers acknowledge after durable persistence.
- Tenant identity is derived from signed server-side authentication context, never trusted from client-supplied tenant IDs.
- The LLM will not receive direct database access. It may only request explicitly authorized application tools in a later phase.

## Local development

1. Copy the environment example file to .env and set a unique APP_SECRET_KEY (at least 32 characters).
2. Generate a Fernet key with the cryptography package and set WHATSAPP_TOKEN_ENCRYPTION_KEY.
3. Set the WhatsApp verification token, Meta app secret and a long random PLATFORM_ADMIN_KEY when configuring webhook integration.
4. Start PostgreSQL with Docker Compose.
5. Install the project and development dependencies.
6. Run Alembic migrations.
7. Start the API with Uvicorn.
8. Start the background worker separately with python -m app.worker.
9. Run pytest.

Endpoints:
- GET /health/live: process liveness only.
- GET /health/ready: checks PostgreSQL connectivity.
- POST /auth/register: creates a business tenant and its initial owner.
- POST /auth/login: authenticates an owner using business slug, email and password.
- GET /auth/me: returns the authenticated account and tenant identifiers.
- POST /services, GET /services: manage and list a tenant's service catalogue.
- PUT /business-hours, GET /business-hours: replace and retrieve the tenant's weekly opening schedule.
- POST /appointments, GET /appointments: create and list tenant appointments.
- POST /appointments/{id}/cancel: cancel a confirmed appointment.
- POST /products, GET /products: manage and list a tenant's retail catalogue.
- POST /orders, GET /orders: create idempotent order drafts and list tenant orders.
- POST /orders/{id}/approve: owner/manager approval and atomic stock deduction.
- POST /orders/{id}/cancel: cancel an order awaiting approval.
- POST /orders/{id}/fulfill: mark an approved order fulfilled.
- GET /webhooks/whatsapp: Meta webhook verification challenge.
- POST /webhooks/whatsapp: signature-verified, deduplicated and encrypted event ingestion.
- POST /internal/whatsapp/connections: internal platform-admin provisioning endpoint; excluded from public OpenAPI.

Prices are represented as integer minor currency units (kobo) and currency is currently fixed to NGN. Business hours currently support one opening interval per weekday. Appointment creation requires a client-generated idempotency UUID; replays with the same request return the original booking, while key reuse with a different payload is rejected. Appointment timestamps must include a timezone offset and are stored in UTC. Retail orders are drafts pending owner/manager approval; stock is deducted only at approval. Payment, delivery and post-approval refunds are outside this increment.

## Security status and limitations

- Passwords are hashed using Argon2.
- Access tokens are signed with HS256 and contain only user and tenant identifiers plus standard claims.
- Authenticated requests reload the user and tenant from PostgreSQL and reject disabled accounts or suspended tenants.
- Tenant-scoped composite foreign keys prevent appointments and orders from referencing another tenant's records.
- Appointment and order writes lock the tenant row, preventing conflicting booking and inventory state transitions for one business. This intentionally serializes tenant writes and is acceptable only for the initial pilot scale.
- Service, business-hours, appointment, product and order changes write audit events in the same database transaction.
- The generic job queue supports idempotent enqueueing, bounded retries, expiring worker leases, and dead-letter state. Job handlers must be idempotent because a worker can lose its lease after an external side effect.
- WhatsApp access tokens are encrypted with Fernet. The encryption key must be stored separately in a secret manager and backed up securely; losing it makes stored tokens unrecoverable.
- The internal WhatsApp connection provisioning endpoint is protected by a shared platform-admin key. This is a pilot provisioning mechanism, not a replacement for a proper Meta Embedded Signup and platform operator identity workflow.
- Inbound WhatsApp events are encrypted, deduplicated and queued. Text and non-text inbound messages are normalized into tenant-scoped conversations and message history; customer phone numbers and text bodies are encrypted at rest, with a keyed hash used for conversation lookup. Automated replies and AI orchestration are not implemented yet.
- Outbound text messaging, durable outbound outbox delivery and delivery-status reconciliation are implemented in the current draft branch. Text sends are restricted to the 24-hour customer-service window and blocked for contacts who send STOP/UNSUBSCRIBE/END/QUIT; approved templates outside that window, credential rotation and live Meta delivery verification remain outstanding.
- Registration/login rate limiting, email verification, password reset, refresh-token rotation, full audit-log access/retention controls and automated data-subject deletion are not yet implemented.
- Appointment rescheduling, customer self-service and resource/staff-specific scheduling are not yet implemented.
- Do not expose this API publicly as a production service until those controls and the remaining Phase 1 exit criteria are complete.
- The local default secret is rejected when APP_ENV=production; production must inject unique secrets through a secret manager.

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
- WhatsApp webhook verification, signature validation, encrypted event persistence and idempotency are tested.
- Background jobs support bounded retries, worker leases and dead-letter handling.
- Audit events cover security-sensitive and business-critical state transitions.
- Unit, integration and end-to-end tests pass against PostgreSQL.
- Operational limits, backup/restore and deployment smoke tests are evidenced.
