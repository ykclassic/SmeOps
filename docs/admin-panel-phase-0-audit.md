# SmeOps Admin Panel — Verified Phase 0 Source Audit

**Repository:** ykclassic/SmeOps  
**Audited branch:** main  
**Audited commit:** 3d6e2320783cd6520943531a97094eea7e8e2a97  
**Audit date:** 2026-11-10  
**Status:** Source-backed audit of the files listed below; not a security certification.

## Executive findings

The repository already has several reusable foundations, but it does not yet have a human-operated platform admin panel or administrative RBAC.

The most important correction to the initial proposal is that tenant state already exists. The current persisted tenant states are `pending`, `active`, and `suspended`. A voluntary `deactivated` state is missing. This is an additive lifecycle change, not a greenfield state model.

A legacy platform operations surface also exists: `GET /internal/ops/summary`, protected by the shared `PLATFORM_ADMIN_KEY` via `X-Platform-Admin-Key`. It exposes aggregate job, worker, WhatsApp outbound/webhook and token-expiry metrics. It is not a user/role-based admin panel.

## Verified repository facts

### Tenant and user model

- `app/models/tenant.py` defines `Tenant.status` as `Literal["active", "suspended", "pending"]`.
- The model-level check constraint accepts only those three values.
- `migrations/versions/0001_create_tenants.py` creates the same three-state database constraint.
- `app/models/user.py` defines tenant-scoped users with roles `owner`, `manager`, `staff` and user states `active`, `disabled`.
- There is no platform-admin role in the current User role constraint.
- `app/api/dependencies.py` authenticates a bearer token and requires both the user and tenant to be active. It has a generic tenant-role dependency via `require_roles`, but no platform RBAC or platform-admin principal.

### Existing operations endpoint

- `app/api/ops.py` defines `GET /internal/ops/summary`.
- It authenticates using a configured `PLATFORM_ADMIN_KEY` and constant-time `hmac.compare_digest`.
- It returns aggregate queue counts/age, worker heartbeat state, WhatsApp outbound counts, webhook counts and WhatsApp token-expiry counts.
- `tests/test_ops_summary.py` verifies the key gate, response shape and that token/customer phone fields are not returned.
- The route is included by `app/main.py`.
- This is a useful operational foundation, but a single shared secret is not sufficient for the planned human admin panel: it has no individual identity, role separation, session lifecycle or per-action audit trail.

### Audit records

- `app/models/business.py` contains `AuditEvent`.
- It is tenant-scoped and records event type, subject type/id, JSON payload, optional actor user id and timestamp.
- Its composite actor foreign key is tenant-bound to `users(tenant_id,id)`.
- Business operations already write events such as service creation, business-hours replacement and appointment lifecycle events.
- This model cannot directly represent platform-wide actions by an administrator who is not a tenant user without a deliberate schema extension or a separate platform audit table. Do not bypass the existing FK.

### Booking and channel enforcement

- `app/api/business.py` has `_lock_tenant`, which selects and locks a tenant only when `Tenant.status == "active"`. Business API writes using this helper are blocked for non-active tenants.
- `app/services/salon_booking.py` checks tenant state in both availability lookup and appointment creation; it rejects a tenant unless its state is `active`.
- `app/services/conversation_engine.py` loads the tenant for channel conversations but the initial load only checks that the tenant exists, not that it is active. Its booking operations later use the booking service, which rejects non-active tenants. This means the booking write is protected, but channel processing may continue further than necessary before rejection.
- Telegram webhook routing resolves a tenant from owner-link or customer booking references in `app/api/telegram.py`; tenant lifecycle enforcement should be centralized in the downstream conversation/booking service rather than relying only on webhook resolution.
- WhatsApp inbound webhook processing in `app/api/whatsapp.py` resolves an active WhatsApp connection and persists/queues the event. It does not itself require the associated tenant to be active.
- Telegram and WhatsApp workers persist inbound events and dispatch conversation jobs. A robust lifecycle gate must revalidate the tenant state at processing and final booking commit time, not only at webhook receipt.
- Existing tenant row locking in booking creation is a useful concurrency foundation. Lifecycle transitions should use the same row-locking discipline so a deactivation racing a booking cannot allow a post-deactivation booking commit.

### Frontend

- The current frontend is a single Next.js owner dashboard in `frontend/app/page.tsx`.
- It includes business registration/login, appointments, services, business hours, Telegram and WhatsApp integration views.
- No dedicated admin dashboard, admin navigation or account-management/deactivation UI was found in the inspected page.
- `frontend/app/api/[...path]/route.ts` is a same-origin BFF allowlist. New owner account lifecycle and admin routes must be explicitly added to this allowlist, with server-side authentication and CSRF/origin protections preserved.
- The browser session is stored in an HttpOnly, SameSite=Strict cookie by the BFF. Do not introduce admin tokens into browser localStorage.

### Migrations and tests

- Current migration tree contains migrations 0001 through 0022; the current tenant-state constraint originates in migration 0001.
- There are tests for auth, operations summary, booking APIs, channel conversations, Telegram inbound, WhatsApp webhooks and worker processing.
- No account deactivation/reactivation, platform admin RBAC, or tenant-state channel acceptance test was identified in the inspected test inventory.
- The repository README documents MFA, email verification, password reset and refresh-token rotation as not implemented. This is a material prerequisite/risk for privileged admin accounts.

## Findings and engineering consequences

| Finding | Evidence | Consequence |
|---|---|---|
| Tenant lifecycle already has pending/active/suspended | tenant model + migration 0001 | Extend existing enum/check; do not introduce a duplicate status field. |
| Voluntary deactivation absent | current tenant states + owner UI/API inspection | Add `deactivated` with owner-only transitions. |
| Tenant-role RBAC exists only within a tenant | User model + dependencies | Platform roles need a separate principal/role model or another explicit trust boundary. Do not add `platform_admin` casually to tenant User.role. |
| Legacy shared-key ops endpoint exists | ops.py + tests | Retain only as a restricted internal/transition surface; do not use as admin UI auth. |
| Tenant audit events exist | AuditEvent model | Add a distinct platform audit event design for cross-tenant administrative actions. |
| Booking write paths check active state | salon_booking.py + business API | Preserve and centralize this enforcement; add race-condition tests. |
| Channel ingestion/processing can proceed for inactive tenant | WhatsApp webhook and conversation engine inspection | Gate channel event processing and outbound booking responses while preserving safe status notices. |
| Worker jobs are durable and tenant-scoped | jobs/worker.py, jobs/telegram.py, jobs/whatsapp.py | Revalidate state in workers and booking commit path; decide how already queued jobs are handled. |
| Owner UI is a single dashboard with BFF allowlist | frontend page + route handler | Add account-management UI and explicit route allowlisting. |
| No admin UI/RBAC found | inspected app routes, models, frontend and tests | Admin panel is a new feature, not an existing capability to expose. |

## Security/design decisions required before coding

1. Keep tenant business states as `pending`, `active`, `deactivated`, `suspended`.
2. Treat owner deactivation and platform suspension as different actions with different authorities and audit reasons.
3. Owner may reactivate only `deactivated`; owner cannot reverse `suspended`.
4. Use a centralized lifecycle service and lock the tenant row for every state transition.
5. Recheck tenant state in webhook processing, conversation processing, and the final booking transaction. A stale queued job must not create a booking after deactivation.
6. Preserve appointments, customers, services, business hours and integration configuration during deactivation.
7. Do not automatically cancel upcoming appointments or send bulk messages as a side effect.
8. Keep sign-in available to an owner of a deactivated tenant so the owner can reactivate. Current `get_current_user` rejects any non-active tenant, so this must change carefully: authenticate active users for the deactivated tenant but gate normal business operations separately. Suspended tenants must remain blocked from owner reactivation.
9. Do not let a user set their own tenant status through generic business profile APIs.
10. Use a separate platform administrator identity/role and platform audit trail. Existing tenant-scoped AuditEvent is not an adequate global-admin audit log.
11. Reassess admin MFA and recovery before granting broad platform access.
12. Avoid returning sensitive customer data or messaging secrets in admin list/detail endpoints.

## Phase 0 backlog disposition

- ADM-001 — AuthN/AuthZ audit: **completed for current source baseline**; retain issue for design review and threat-model sign-off.
- ADM-002 — Business/user model audit: **completed for current source baseline**.
- ADM-003 — Audit model audit: **completed for current source baseline**.
- ADM-004 — Legacy provisioning/ops endpoint audit: **partially completed**; `/internal/ops/summary` verified. Search all references and inspect any legacy provisioning endpoint/config before closing.
- ADM-005 — Booking/messaging entry-point audit: **completed for the inspected current routes and workers**, with additional regression coverage required.
- ADM-006 — Lifecycle migration design: **proposed by this audit**, pending review before migration implementation.

This audit is based on repository source at the audited commit. It does not prove staging or production deployment state.
