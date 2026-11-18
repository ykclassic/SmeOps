# SmeOps Platform Admin Panel — PRD and Prioritised Engineering Backlog

**Status:** Requirements baseline  
**Owner:** TechSolute Hub  
**Repository:** ykclassic/SmeOps  
**Related audit:** `docs/admin-panel-phase-0-audit.md`

## 1. Product objective

Provide a secure, separate administrative interface for TechSolute Hub to manage businesses, users, platform operations, integrations, support and future commercial capabilities. Use the existing SmeOps backend and PostgreSQL database as the source of truth. Do not create a second tenant/business database.

## 2. Product surfaces

### Business dashboard
A business owner manages only their own business: services, hours, appointments, customers, messaging integrations and account lifecycle.

### Platform admin panel
Authorized TechSolute Hub personnel manage platform-wide business records, platform users, operational health, support, security, configuration and future subscriptions.

The admin panel must not provide unrestricted impersonation or unlogged access to customer records.

## 3. Business account lifecycle requirements

The existing Tenant state is `pending | active | suspended`. Extend it to:

- `pending`: onboarding/incomplete provisioning.
- `active`: business can operate.
- `deactivated`: business owner voluntarily paused the SmeOps account.
- `suspended`: platform administrator restricted the account.

### Owner actions
- An authorized owner can transition `active → deactivated`.
- An authorized owner can transition `deactivated → active`.
- An owner cannot clear `suspended`.
- Require explicit confirmation and recent authentication/reauthentication for sensitive state changes.
- Preserve sign-in for deactivated owners so they can reactivate. Keep normal business operations blocked.
- Do not permit manager/staff users to change tenant lifecycle unless a later policy explicitly grants it.

### Admin actions
- An authorized operations/super admin can transition `active → suspended` and `deactivated → suspended`.
- An authorized admin can transition `suspended → active` after recording a reason.
- All admin transitions require an individual admin identity, permission check and audit record.

### Effects of deactivation/suspension
- Reject new appointments through direct business API, Telegram and WhatsApp.
- Revalidate tenant state in durable workers and at final booking commit; a job queued before deactivation must not commit a new appointment after deactivation.
- Stop new booking conversations for inactive tenants with a neutral, non-sensitive unavailable response where appropriate.
- Preserve existing appointments, customers, services, business hours, tenant identifiers and integration configuration.
- Do not auto-cancel upcoming appointments, delete data, disconnect external channels or send bulk customer messages.
- Allow owner sign-in and access to account management only for voluntary deactivation. Suspended accounts remain blocked from normal sign-in/reactivation; admin remediation is required.
- Reactivation restores normal availability without re-registration. Reauthorize an external integration only if its own provider state has expired/revoked.

### Concurrency and audit
- Centralize transitions in a lifecycle service.
- Lock tenant row during transitions and booking commit.
- Write state transition and audit record atomically.
- Make repeated identical transitions idempotent.
- Record actor type/id, previous state, new state, reason, timestamp and request/correlation ID.
- Keep global platform audit records separate from tenant AuditEvent unless schema changes make global actor semantics explicit.

## 4. Admin roles

Initial roles: `super_admin`, `operations_admin`, `support_admin`, `finance_admin`.

Use deny-by-default backend RBAC. Do not overload tenant-scoped `User.role` with platform roles. Privileged admin authentication should have MFA before broad access is enabled. Never expose passwords, tokens or messaging secrets.

## 5. Admin panel modules

1. Overview: tenant counts, bookings, integrations and operational summaries.
2. Businesses: search, details, onboarding state, activation/deactivation/suspension history.
3. Users: accounts, roles, session revocation and authentication events.
4. Analytics: platform metrics with tenant-scoped breakdown and privacy controls.
5. Integrations: Telegram/WhatsApp connection health, webhook and delivery errors; secrets remain masked.
6. System health: API, DB, workers, queues, dead-letter jobs, webhook processing and migration state.
7. Support: tickets, assignment, notes, escalation and resolution.
8. Security & audit: admin access, privileged actions, security events and incident records.
9. Configuration: feature flags, channel availability, maintenance mode, templates and global limits.
10. Subscriptions: plans, trials, payment history and revenue reporting (later phase).

## 6. Non-functional requirements

- Tenant isolation is enforced server-side and covered by cross-tenant tests.
- Admin permissions are enforced on every endpoint, not only in the UI.
- Sensitive state changes are audited transactionally.
- No secrets or unnecessary PII in admin API responses/logs.
- Pagination and bounded filters for large tenant/user/job lists.
- Index tenant state and admin search fields only where query plans justify them.
- Use UTC for audit timestamps.
- Preserve existing API compatibility and Telegram/WhatsApp functionality.
- CI green, migration validation, staging deployment and real acceptance are separate gates.
- No production-ready claim based on CI alone.

## 7. Prioritised engineering backlog

### Phase 0 — source audit and design gate
- ADM-001: Authentication/authorization and privilege-boundary audit.
- ADM-002: Tenant/user schema and relationship audit.
- ADM-003: Existing audit-event schema and write-path audit.
- ADM-004: Legacy platform provisioning and shared-key operations endpoint security audit.
- ADM-005: End-to-end inventory of all business booking, webhook, conversation and worker entry points.
- ADM-006: Review and approve account lifecycle state machine, migration and audit design.

### Phase 1 — business account lifecycle (implement after ADM-006 sign-off)
- ADM-007: Add `deactivated` to Tenant model, database check constraint and migration; preserve existing statuses.
- ADM-008: Build centralized, transaction-safe lifecycle transition service and transition validation.
- ADM-009: Add owner deactivation endpoint with owner-only authorization, confirmation/reauthentication policy and optional reason.
- ADM-010: Add owner reactivation endpoint for `deactivated` only; keep suspended accounts blocked.
- ADM-011: Enforce account state on direct business booking APIs and appointment creation.
- ADM-012: Enforce state at Telegram webhook/context resolution and conversation processing; return a safe unavailable response.
- ADM-013: Enforce state for WhatsApp inbound events and conversation processing.
- ADM-014: Revalidate tenant state in worker jobs and final booking transaction; define safe handling for queued jobs and retries.
- ADM-015: Add atomic lifecycle audit records with actor, previous/new state, reason, timestamp and correlation ID.
- ADM-016: Add owner-facing Account Management UI and BFF allowlist routes; include confirmation, status display and reactivation.

### Phase 2 — platform admin foundation
- ADM-017: Platform administrator identity and role model.
- ADM-018: Backend RBAC policies and permission tests.
- ADM-019: Secure initial Super Admin provisioning and MFA gate.
- ADM-020: Protected admin API and dashboard route boundaries.
- ADM-021: Admin dashboard shell and navigation.
- ADM-022: Business search/list/detail APIs and UI.
- ADM-023: Administrative suspension/reactivation APIs.
- ADM-024: Business lifecycle history and audit viewer.

### Phase 3 — operations/security
- ADM-025: Platform audit log and admin action viewer.
- ADM-026: Worker/queue/dead-letter monitoring.
- ADM-027: Telegram and WhatsApp health monitoring.
- ADM-028: Platform user management and session revocation.
- ADM-029: Security event monitoring and alert integration.

### Phase 4 — platform management
- ADM-030: Platform analytics.
- ADM-031: Support ticket management.
- ADM-032: Announcements and maintenance notifications.
- ADM-033: Feature flag and global configuration management.

### Phase 5 — commercial administration
- ADM-034: Subscription plan management.
- ADM-035: Subscription assignment and trial management.
- ADM-036: Payment history and reconciliation.
- ADM-037: Revenue analytics.

## 8. Definition of done

Each feature must have backend tests, frontend type-check/build where applicable, migration upgrade/downgrade validation, permission tests, tenant-isolation tests and regression coverage. CI must pass before merge. Staging deployment must be checked against the deployed SHA and migration head. Real Telegram/WhatsApp acceptance is required for channel changes. No production deployment or merge to main is implied by this PRD.
