# SmeOps

SmeOps is a multi-tenant WhatsApp-based AI customer assistant SaaS for appointment-based and retail businesses, starting in Nigeria.

## Delivery status

**Phase 1 — Core Platform and Business Operations: in progress.**

This repository is being established from an empty baseline. The first implementation increment creates the backend foundation and automated quality gates. Production readiness is not claimed until the phase exit criteria are demonstrated.

## Architecture baseline

- Python 3.12 and FastAPI for the API.
- PostgreSQL 16 for durable application state.
- SQLAlchemy 2.x and Alembic for persistence and schema migrations.
- Deterministic backend services own bookings, orders, authorization and state transitions.
- WhatsApp Cloud API integration is isolated behind an adapter.
- Background work is persisted and processed by workers; webhook handlers acknowledge quickly.
- Tenant identity is derived from authenticated server-side context, never trusted from client-supplied tenant IDs.
- The LLM will not receive direct database access. It may only request explicitly authorized application tools in a later phase.

## Local development

1. Copy `.env.example` to `.env` and set a strong `APP_SECRET_KEY` and local database URL.
2. Start PostgreSQL: `docker compose up -d db`.
3. Install: `python -m pip install -e '.[dev]'`.
4. Run migrations: `alembic upgrade head`.
5. Start API: `uvicorn app.main:app --reload`.
6. Run tests: `pytest`.

The API health endpoint is `GET /health/live`. Readiness checks are separate and will include dependency checks before deployment.

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

See the product blueprint and phase roadmap in project documentation as they are added.
