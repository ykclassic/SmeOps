# SmeOps Admin Panel — Phase 0 Source Audit

**Repository:** ykclassic/SmeOps  
**Audited ref:** `main`  
**Audited commit:** `3d6e2320783cd6520943531a97094eea7e8e2a97`  
**Audit date:** 2026-11-10  
**Scope:** ADM-001 through ADM-006; source-level review of account state, authentication, tenant isolation, audit events, operations access, booking channels, workers and frontend routing.

## Executive finding

SmeOps already has useful platform foundations, but it does not yet have a human-operated platform administration system or a complete business account lifecycle. The tenant model currently supports `active`, `suspended`, and `pending`; it does not support voluntary `deactivated`. Authentication denies access when a tenant is not active, which means a deactivated owner could not use the current authentication path to reactivate. More importantly, queued channel conversations and inbound webhook acceptance do not consistently re-check tenant state.

The account lifecycle must therefore be implemented as a backend-wide policy, not a dashboard-only toggle.

## 1. Verified source inventory

| Area | Source | Verified state |
|---|---|---|
| Tenant model | `app/models/tenant.py` | `Tenant.status` is constrained to active/suspended/pending. No deactivated state or lifecycle metadata. |
| Tenant schema migration | `migrations/versions/0001_create_tenants.py` | Database CHECK constraint mirrors active/suspended/pending. |
| User model | `app/models/user.py` | Roles are owner/manager/staff; user status is active/disabled. No platform admin role. |
| User migration | `migrations/versions/0002_create_users.py` | Tenant-scoped user identity and role/status constraints. |
| Registration/login | `app/api/auth.py` | Registration creates active tenant and owner. Login requires active tenant and active user. |
| Auth dependency | `app/api/dependencies.py` | Every CurrentUser lookup requires active user and active tenant. Role helper only checks tenant roles. |
| Business API | `app/api/business.py` | Mutating operations use an active-tenant row lock helper. Appointment creation also locks tenant. Reads generally rely on CurrentUser. |
| Retail API | `app/api/retail.py` | Mutating product/order flows use an active-tenant row lock helper. |
| Tenant audit model | `app/models/business.py` | AuditEvent is tenant-scoped; actor is constrained to a user in the same tenant; event, subject, JSON payload and timestamp are stored. |
| Audit usage | `app/api/business.py`, `app/services/whatsapp_onboarding.py`, other services | Existing events cover operational actions such as service creation, business-hours replacement, appointments and WhatsApp connection lifecycle. |
| Internal operations | `app/api/ops.py` | `GET /internal/ops/summary` uses a shared `X-Platform-Admin-Key`, returns aggregate worker/job/WhatsApp metrics. This is a machine-secret gate, not human RBAC. |
| Legacy provisioning | `app/api/whatsapp.py` | Hidden-schema `POST /internal/whatsapp/connections` uses the same platform key and writes a tenant-scoped audit event with a null user actor. |
| Configuration | `app/core/config.py` | `PLATFORM_ADMIN_KEY` is required in production. |
| Health | `app/api/health.py` | Liveness, DB readiness and worker heartbeat endpoints exist. |
| Worker | `app/jobs/worker.py` | Durable jobs are processed; failures log tracebacks without logging job payloads. |
| Conversation processing | `app/services/conversation_engine.py` | Worker loads Tenant by ID and checks existence, but the reviewed path does not reject non-active tenant state before continuing conversation logic. |
| WhatsApp webhook | `app/api/whatsapp.py` | Verifies Meta signature, maps active connection to tenant, persists event and queues work; tenant status is not checked in the reviewed ingress path. |
| Telegram platform webhook | `app/api/telegram.py` | Verifies platform bot secret, resolves tenant from booking reference or chat context, persists event and queues work; tenant status is not checked in the reviewed tenant-resolution path. |
| Frontend | `frontend/app/page.tsx` | One owner-facing dashboard page with business tabs; no separate admin interface found in the tree. |
| Frontend BFF | `frontend/app/api/[...path]/route.ts` | Explicit API allowlist contains owner/business routes only; no platform admin routes are exposed through the proxy. |
| Existing tests | `tests/test_ops_summary.py`, `tests/test_auth_api.py`, `tests/test_health.py`, etc. | Ops summary authorization/redaction and auth/health are tested. No account deactivation, admin RBAC or platform admin dashboard tests were found in the repository test tree. |

## 2. ADM-001 — Authentication and authorization audit

**Finding: partially implemented foundation; no platform-admin authorization.**

- Bearer access tokens are decoded in `app/api/dependencies.py`.
- The dependency reloads the user from the database and verifies user and tenant status.
- `require_roles()` only supports existing tenant roles: owner, manager, staff.
- The current login endpoint requires `Tenant.status == "active"`.
- `CurrentUser` also requires active tenant state.

**Lifecycle consequence:** Simply adding `deactivated` to the tenant CHECK constraint would lock the owner out of the application, including the reactivation path. Authentication must distinguish a voluntarily deactivated tenant from an administratively suspended tenant. A restricted owner lifecycle session/path is required; normal business operations must remain blocked while deactivated.

**Design constraint:** Do not add platform admin roles to the tenant-scoped User.role enum. Platform administrator identities and permissions must be a separate security boundary.

## 3. ADM-002 — Tenant and user model audit

**Finding: tenant state exists, but not the required lifecycle semantics.**

- Tenant status currently permits active, suspended, pending.
- User status currently permits active, disabled.
- Tenant owns users and business data through tenant IDs.
- Tenant status is already used by auth and active-tenant lock helpers.

**Required schema design:**

- Add voluntary `deactivated` to the tenant status constraint.
- Retain `suspended` as an administrative restriction and `pending` for existing lifecycle compatibility.
- Add appropriate lifecycle timestamps/metadata only after deciding whether they are authoritative or derivable from audit events.
- Keep owner identity and tenant data intact.
- Avoid destructive cascades or account deletion as part of deactivation.

## 4. ADM-003 — Audit-event audit

**Finding: reusable tenant audit foundation exists; platform audit does not.**

The existing `audit_events` table is not a complete platform-admin audit store:
- `tenant_id` is mandatory.
- `actor_user_id` references a user belonging to the same tenant.
- It is indexed by tenant and creation time.
- It cannot cleanly represent a cross-tenant platform administrator as actor.
- No platform-wide audit listing interface was found.

Use existing AuditEvent for business-owner lifecycle actions where suitable. Introduce a separate platform audit record for administrative actions when admin RBAC is implemented. Do not claim database-enforced immutability: the reviewed model does not itself prevent UPDATE/DELETE.

## 5. ADM-004 — Legacy platform key audit

**Finding: operational machine-key access exists; human admin RBAC does not.**

- `/internal/ops/summary` checks `X-Platform-Admin-Key` with constant-time comparison.
- `/internal/whatsapp/connections` uses the same key for legacy provisioning.
- These endpoints are not a user/role-based admin experience.
- Production configuration requires `PLATFORM_ADMIN_KEY`.

Actions:
1. Retain the key only for narrowly scoped machine/internal operations during transition.
2. Inventory all usages and consumers before changing it.
3. Ensure the key is not exposed to browser code or business owners.
4. Move human operations to authenticated platform-admin identities and backend RBAC.
5. Keep internal machine endpoints separate from human admin routes.

## 6. ADM-005 — Booking, messaging and worker enforcement audit

**Finding: enforcement is incomplete across asynchronous and inbound paths.**

- Business and retail mutating API paths use an active-tenant row lock helper.
- The auth dependency blocks a tenant that is not active from ordinary authenticated APIs.
- WhatsApp ingress checks that the WhatsApp connection is active, not that the tenant is active, before persisting/queuing an event.
- Telegram platform ingress resolves tenant context/reference and queues work without a tenant-active check in the reviewed path.
- Conversation worker loads the tenant and checks that it exists; the reviewed code does not check that its status is active before proceeding.
- Therefore, tenant deactivation cannot be considered safe until ingress, queue processing and final booking writes all enforce the state.
- Retail order writes have an active-tenant lock at the API boundary; verify all other write paths and queued paths as part of implementation.

**Required enforcement points:**
1. Central lifecycle policy/service.
2. Authenticated business API mutations.
3. Telegram and WhatsApp inbound tenant resolution/acceptance.
4. Conversation worker immediately before processing.
5. Final appointment/order transaction, with tenant row lock or equivalent concurrency-safe state check.
6. Outbound notification rules for jobs already queued at transition time.

**Race condition to address:** A booking may be in flight while deactivation commits. Use a shared tenant-row locking/transaction strategy so a booking cannot commit after a successful deactivation transition. Rechecking only at webhook ingress is insufficient.

## 7. ADM-006 — Lifecycle schema and transition design

**Recommended lifecycle semantics:**

- `active`: normal business operations permitted.
- `deactivated`: voluntary owner-initiated state; no new bookings/orders; owner retains a narrow route to sign in and reactivate.
- `suspended`: administrator-imposed state; owner cannot self-reactivate.
- `pending`: retain existing semantics until registration/onboarding use is fully audited.

**Transition rules:**

| From | Actor/action | To |
|---|---|---|
| active | authorized owner deactivates | deactivated |
| deactivated | authorized owner reactivates | active |
| active or deactivated | authorized platform admin suspends | suspended |
| suspended | authorized platform admin lifts suspension | active |
| suspended | owner attempts self-reactivation | rejected |

The initial owner lifecycle feature can ship before platform admin RBAC, but administrative suspension must remain inaccessible until a secure admin identity/permission layer exists.

**Data behavior:** Preserve appointments, customers, services, products, orders, integration configuration and history. Do not automatically cancel appointments or delete customer records. Existing upcoming appointments remain recorded; owner should be warned before deactivation.

**Audit behavior:** Persist lifecycle transition and corresponding audit event atomically. Owner events belong in tenant audit history. Platform-admin actions require a platform audit store with actor identity, target tenant, action, reason, timestamp and before/after state.

## 8. Frontend and test findings

- The current frontend root is a business-owner dashboard, not an admin panel.
- The BFF has an explicit allowlist; admin paths must be added intentionally and must preserve HttpOnly session handling and origin checks.
- Existing operations test verifies missing/invalid platform key rejection and checks aggregate output for absence of access-token and customer-phone fields.
- No tests named or scoped for tenant deactivation/reactivation, admin RBAC, platform-wide business management or account-state webhook/worker rejection were found.

## 9. Risks and required controls

| Risk | Severity | Required control |
|---|---|---|
| Owner locked out after deactivation | Critical | Separate lifecycle authentication/authorization from active-business access. |
| Queued job books after deactivation | Critical | Worker and final write transaction revalidate tenant state under a concurrency-safe lock. |
| Inbound webhook keeps queuing work | High | Tenant-state check at Telegram/WhatsApp ingress and safe acknowledgement semantics. |
| Admin key mistaken for RBAC | High | Separate machine credential from human admin identity and permission model. |
| Tenant audit misused for global admin events | High | Separate platform audit model. |
| In-flight booking races with deactivation | Critical | Serialize state transition and booking commit against the tenant row. |
| Outbound messages after deactivation | Medium | Define and test treatment of pre-existing queued customer/owner notifications. |
| Existing upcoming appointments ambiguous | Medium | Preserve them and clearly communicate policy; do not auto-cancel. |

## 10. Phase 0 disposition

ADM-001 through ADM-006 have been source-reviewed and findings recorded in this document. Before implementation, the design review must confirm the exact authentication behavior for deactivated owners, transaction/locking approach, webhook acknowledgment policy and queued outbound behavior. This document is the evidence baseline; it is not proof of runtime or staging acceptance.

## Source references

- [Tenant model](../app/models/tenant.py)
- [Tenant migration](../migrations/versions/0001_create_tenants.py)
- [User model](../app/models/user.py)
- [Auth API](../app/api/auth.py)
- [Auth dependencies](../app/api/dependencies.py)
- [Business API](../app/api/business.py)
- [Retail API](../app/api/retail.py)
- [Tenant audit model](../app/models/business.py)
- [Internal operations API](../app/api/ops.py)
- [WhatsApp API](../app/api/whatsapp.py)
- [Telegram API](../app/api/telegram.py)
- [Conversation worker service](../app/services/conversation_engine.py)
- [Worker loop](../app/jobs/worker.py)
- [Frontend dashboard](../frontend/app/page.tsx)
- [Frontend API proxy](../frontend/app/api/[...path]/route.ts)
- [Operations tests](../tests/test_ops_summary.py)
