# SmeOps Admin Panel — Engineering Backlog

**Baseline audit:** [Phase 0 Source Audit](admin-panel-phase0-audit.md)  
**Repository:** ykclassic/SmeOps  
**Priority convention:** P0 blocks safe account lifecycle/admin foundations; P1 operational support; P2 platform management; P3 commercial expansion.

## Delivery rules

- Work on feature branches; merge only after GitHub Actions CI is green.
- Do not deploy to staging or production without a separate explicit deployment instruction.
- A merged feature is not operationally accepted until staging and real acceptance tests pass.
- Every privileged operation must be enforced server-side and audited.
- Keep the tenant database as source of truth; platform administrators are a separate identity/permission boundary.
- Preserve free-resource constraints where practical.
- ADM-001–ADM-006 are the immediate design/engineering gate. ADM-007–ADM-016 follow in order after the Phase 0 design review is signed off.

## Phase 0 — Audit and architecture gate (immediate)

### ADM-001 — Finalize authentication and authorization boundary
**Priority:** P0  
**Status:** Source audit complete; design decision review required.  
**Scope:** Document current auth dependency behavior; specify restricted authentication for owners of deactivated tenants; specify that suspended owners cannot self-reactivate; define separation of tenant roles and platform-admin identities.  
**Acceptance criteria:**
- Auth flow diagram covers active, deactivated and suspended tenants.
- Deactivated owner can authenticate only to lifecycle/status endpoints.
- Normal tenant APIs reject deactivated tenants.
- Suspended tenant owner cannot self-reactivate.
- Platform-admin identity is not represented as a tenant user role.

### ADM-002 — Finalize tenant/user schema and lifecycle metadata
**Priority:** P0  
**Status:** Source audit complete; migration design required.  
**Scope:** Review Tenant/User models and migrations; define status CHECK constraint changes and any lifecycle metadata. Preserve existing pending/suspended semantics and all existing tenant records.  
**Acceptance criteria:**
- Forward-only Alembic migration design reviewed.
- Existing statuses and data remain valid.
- New deactivated status has DB-level validation.
- Downgrade behavior is safe and documented.
- No customer/appointment data deletion.

### ADM-003 — Define tenant and platform audit strategy
**Priority:** P0  
**Status:** Source audit complete; platform audit design required.  
**Scope:** Reuse tenant AuditEvent for owner lifecycle actions where appropriate; design a separate platform audit record for cross-tenant admin actions.  
**Acceptance criteria:**
- Owner lifecycle event schema and payload are defined.
- Platform admin event records actor, target tenant, action, reason, timestamp and before/after state.
- Sensitive payloads exclude secrets and customer PII.
- Atomicity and retention requirements are documented.
- No claim of append-only/immutable storage without an enforced mechanism.

### ADM-004 — Review and contain legacy platform-key operations
**Priority:** P0  
**Status:** Source audit complete; migration/containment plan required.  
**Scope:** Inventory `PLATFORM_ADMIN_KEY` consumers and separate machine-to-machine endpoints from human admin access.  
**Acceptance criteria:**
- Every consumer and operational caller is identified.
- Key remains server-side and absent from frontend/browser responses.
- Human admin functions do not use shared static key authentication.
- Rotation and eventual retirement plan is documented.
- Existing provisioning and ops tests remain green.

### ADM-005 — Complete account-state enforcement map
**Priority:** P0  
**Status:** Source audit complete; implementation matrix required.  
**Scope:** Map all API, Telegram, WhatsApp, retail, conversation worker, final write and outbound job paths that can mutate tenant data or create customer-visible effects.  
**Acceptance criteria:**
- Every entry point is listed with enforcement location.
- Webhook behavior for inactive tenants is specified and idempotent.
- Workers revalidate state before processing and before final writes.
- Deactivation and booking transactions are serialized to prevent races.
- Queued outbound message behavior is specified.

### ADM-006 — Approve lifecycle state machine and migration design
**Priority:** P0  
**Status:** Initial design in audit; final review gate.  
**Scope:** Approve active/deactivated/suspended transitions, preserve pending compatibility, owner and admin permissions, upcoming appointment policy, and reactivation behavior.  
**Acceptance criteria:**
- State transition table approved.
- Reauthentication/confirmation policy approved.
- Existing upcoming appointments remain preserved; no silent cancellation.
- Migration and rollback/data recovery strategy approved.
- Product and engineering agree on queued work and outbound message semantics.

## Phase 1 — Account lifecycle implementation

### ADM-007 — Add persisted deactivated tenant state
**Priority:** P0  
**Depends on:** ADM-001–ADM-006  
**Scope:** Add `deactivated` to Tenant type and database constraint; add reviewed lifecycle metadata if approved.  
**Acceptance criteria:** Migration passes on empty and populated staging-like databases; existing active/suspended/pending rows remain unchanged; model/schema metadata checks pass.

### ADM-008 — Implement centralized lifecycle transition service
**Priority:** P0  
**Depends on:** ADM-007  
**Scope:** Implement validated, transactional, idempotent owner deactivation/reactivation transitions and audit writes.  
**Acceptance criteria:** Invalid transitions fail closed; repeated requests are idempotent; state and audit commit atomically; concurrency tests cover duplicate and competing transitions.

### ADM-009 — Implement owner deactivation API
**Priority:** P0  
**Depends on:** ADM-008  
**Scope:** Owner-only endpoint; explicit confirmation; recent reauthentication; optional reason; no destructive side effects.  
**Acceptance criteria:** Manager/staff and cross-tenant requests rejected; owner receives resulting state; sensitive operation cannot be triggered by CSRF/cross-origin request.

### ADM-010 — Implement owner reactivation API and restricted owner session
**Priority:** P0  
**Depends on:** ADM-001, ADM-008  
**Scope:** Permit owner authentication for deactivated (not suspended) tenant; expose only lifecycle/status operations while deactivated; reactivate without re-registration.  
**Acceptance criteria:** Deactivated owner can sign in and reactivate; business APIs remain blocked until reactivation; suspended tenant cannot self-reactivate; stale tokens cannot bypass state checks.

### ADM-011 — Enforce state in REST booking and retail APIs
**Priority:** P0  
**Depends on:** ADM-008  
**Scope:** Apply central active-tenant guard and concurrency-safe tenant row locking to all booking, appointment mutation, product/order creation and other tenant writes.  
**Acceptance criteria:** No new booking/order or business mutation commits after deactivation succeeds; read-only lifecycle access remains available; race tests pass.

### ADM-012 — Enforce state at Telegram inbound boundary
**Priority:** P0  
**Depends on:** ADM-008  
**Scope:** Resolve tenant status before accepting/queuing customer booking work; preserve safe webhook acknowledgment and deduplication semantics.  
**Acceptance criteria:** Deactivated/suspended tenant cannot start or continue booking; tenant state is revalidated in queued processing; other tenants using shared bot remain unaffected.

### ADM-013 — Enforce state at WhatsApp inbound boundary
**Priority:** P0  
**Depends on:** ADM-008  
**Scope:** Check tenant state when mapping connected number to tenant and before queuing customer booking work.  
**Acceptance criteria:** Deactivated/suspended tenant cannot start or continue booking; valid Meta webhook requests are handled safely; no cross-tenant effect.

### ADM-014 — Revalidate state in background workers and final booking transaction
**Priority:** P0  
**Depends on:** ADM-008, ADM-011, ADM-012, ADM-013  
**Scope:** Revalidate state immediately before processing a conversation and again inside the final booking transaction under a concurrency-safe lock.  
**Acceptance criteria:** Jobs queued before deactivation cannot create appointments/orders after deactivation commits; retries are idempotent; no duplicate bookings.

### ADM-015 — Define and enforce queued outbound behavior
**Priority:** P0  
**Depends on:** ADM-006, ADM-008  
**Scope:** Classify already-queued customer confirmations, owner alerts and lifecycle notifications; prevent stale booking confirmations after deactivation while preserving notifications for pre-existing appointments according to approved policy.  
**Acceptance criteria:** Behavior documented per job type; tests prove no false confirmation for rejected bookings; valid pre-existing appointment notifications are not silently discarded.

### ADM-016 — Add business dashboard account management UI
**Priority:** P0  
**Depends on:** ADM-009, ADM-010  
**Scope:** Add Account Management section with deactivation confirmation, state display and reactivation path; clearly explain existing appointment preservation.  
**Acceptance criteria:** Owner can deactivate/reactivate from UI; clear warnings and success/error states; UI cannot override backend authorization; mobile layout works; typecheck/build pass.

## Phase 2 — Platform administrator foundation (after lifecycle)

The following items remain in the broader PRD and should be scheduled after ADM-016:
- Platform-admin identity and RBAC.
- Admin dashboard shell and business directory.
- Administrative suspension/reactivation.
- Platform-wide audit log.
- User management.
- System health, queue and integration monitoring.
- Analytics, support, announcements and configuration.
- Subscription and billing administration.

These are intentionally not mixed into ADM-007–ADM-016. Account lifecycle must be secure before a broad administrative control plane is exposed.
