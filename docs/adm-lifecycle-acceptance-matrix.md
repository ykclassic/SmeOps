# Tenant Lifecycle Acceptance Matrix

**Scope:** ADM-007 through ADM-015 backend lifecycle enforcement, with emphasis on ADM-011–ADM-015 integration.  
**Candidate:** follow-up acceptance branch `feature/adm-notification-acceptance`, based on merged PR #72.  
**Candidate implementation head:** `4e594c153610083e96ca7a866c19db53ae9c3568`.  
**CI evidence:** Follow-up PR #73 head `4e594c153610083e96ca7a866c19db53ae9c3568` passed GitHub Actions CI run #911 (`43624168342`): 252 backend tests passed with 2 Starlette deprecation warnings; Ruff, migration validation, metadata drift, populated-database migration, downgrade-safety, frontend type-check and production build all passed. The final documentation revision must receive its own CI run before merge.  
**Environment:** automated CI against PostgreSQL and the test application. This matrix does not certify staging or production runtime behavior. The staging database migration to `0024_outbound_lifecycle_policy` and both `deliver_after_deactivation` columns were independently verified, but targeted lifecycle staging acceptance remains unrun.

## Status definitions

- **PASS — automated:** a regression test exercises the behavior on the combined candidate branch.
- **PARTIAL:** adjacent tests or code coverage exist, but the full stated acceptance condition is not demonstrated end-to-end.
- **PENDING:** CI or independent review is not yet complete.
- **NOT ACCEPTED:** evidence is insufficient; the row remains a gate rather than being inferred from implementation.

## Lifecycle-state behavior matrix

| Surface / operation | active | deactivated | suspended | pending | Evidence / status |
|---|---|---|---|---|---|
| REST appointment and retail mutations | Allowed | Rejected | Rejected | Rejected | **PASS — automated.** `tests/test_tenant_lifecycle_rest_api.py` covers all four states and verifies rejected writes do not create side effects. |
| REST read-only routes | Existing authenticated read behavior | Rejected by active-tenant auth | Rejected | Rejected | **PASS — automated.** `test_read_only_rest_routes_follow_existing_current_user_authentication_contract`. |
| Appointment creation racing with deactivation | Commit if writer wins serialization | Rejected if deactivation wins | Not a voluntary race target | Not a voluntary race target | **PASS — automated.** PostgreSQL lock-wait test in `tests/test_tenant_lifecycle_rest_api.py`. |
| Retail order creation racing with deactivation | Commit if writer wins serialization | Rejected if deactivation wins | Not a voluntary race target | Not a voluntary race target | **PASS — automated.** PostgreSQL lock-wait test in `tests/test_tenant_lifecycle_rest_api.py`. |
| WhatsApp webhook ingress | Persist and enqueue | Acknowledge, do not persist/enqueue | Acknowledge, do not persist/enqueue | Acknowledge, do not persist/enqueue | **PASS — automated.** `tests/test_whatsapp_webhook_lifecycle.py`, including mixed-tenant batches and tenant-lock race. |
| WhatsApp queued inbound worker | Process | Terminalize stale event; no conversation/message/downstream job | Same | Same | **PASS — automated candidate coverage.** `tests/test_whatsapp_worker_lifecycle.py`; all three inactive states parameterized. |
| WhatsApp conversation worker | Process | Skip without mutating conversation or creating outbound work | Same | Same | **PASS — automated candidate coverage.** `tests/test_whatsapp_worker_lifecycle.py`; all three inactive states parameterized. |
| Telegram queued inbound worker | Process | Terminalize stale event | Same | Same | **PASS — automated candidate coverage.** `tests/test_telegram_lifecycle.py`; all three inactive states parameterized. |
| Telegram conversation worker | Process | Skip before conversation processing | Same | Same | **PASS — automated candidate coverage.** `tests/test_telegram_lifecycle.py`; all three inactive states parameterized. |
| Telegram webhook ingress and tenant-lock race | Persist/enqueue | Reject/ignore after lifecycle lock recheck | Reject/ignore | Reject/ignore | **PENDING CI.** `tests/test_telegram_webhook_lifecycle_races.py::test_actual_telegram_webhook_routes_wait_for_inactive_tenant_and_do_not_enqueue` now parameterizes both platform and legacy routes across deactivated, suspended and pending, asserting the real PostgreSQL lock wait and zero persisted events/jobs.
| Ordinary queued WhatsApp outbound | Deliver subject to provider policy | Suppress and mark failed | Suppress | Suppress | **PASS — automated.** `tests/test_whatsapp_outbound.py`. |
| Preserved WhatsApp appointment outbound | Deliver subject to provider policy | Deliver if explicitly marked and otherwise provider rules permit | Suppress | Suppress | **PASS — worker policy automated; PARTIAL end-to-end event proof.** `tests/test_whatsapp_outbound.py` verifies preserved delivery and inactive-state suppression. The test does not yet prove every real booking/cancel/reschedule path creates the preservation flag. |
| Ordinary queued Telegram outbound | Deliver | Suppress and mark failed | Suppress | Suppress | **PASS — automated.** `tests/test_telegram_outbound.py`. |
| Preserved Telegram appointment outbound | Deliver | Deliver if explicitly marked | Suppress | Suppress | **PASS — worker policy automated; PARTIAL end-to-end event proof.** `tests/test_telegram_outbound.py` verifies preserved delivery and suspended/pending suppression. |
| Telegram owner appointment alerts | Deliver | Deliver durable alerts for appointments committed before deactivation | Suppress | Suppress | **PASS — automated integration candidate.** `tests/test_appointment_notification_lifecycle.py` creates an appointment through REST, verifies its durable notification and job, deactivates the tenant, then runs the real worker with a stubbed provider and verifies delivery. |
| Customer appointment confirmations / cancellation / reschedule replies | Deliver | Preserve only when atomically enqueued with successful appointment mutation | Suppress | Suppress | **PARTIAL; new integration test pending CI.** `tests/test_appointment_notification_lifecycle.py::test_customer_cancellation_reply_delivers_after_deactivation` drives the real Telegram customer cancellation conversation, verifies the committed appointment mutation and preserved customer outbox row, then executes the real outbound worker after deactivation. Booking-confirmation and reschedule variants, and WhatsApp customer delivery, remain separate coverage.
| WhatsApp opt-out and service-window constraints | Enforced | Must not be bypassed by preservation flag | Enforced | Enforced | **PASS — automated policy tests** in `tests/test_whatsapp_outbound.py`; retain provider policy as an independent gate. |
| Ambiguous external send outcome | Never blindly retry | Never blindly retry | Never blindly retry | Never blindly retry | **PASS — automated.** WhatsApp and Telegram worker tests retain `sending → unknown` handling. |
| Deactivated owner authentication | Normal access while active | Lifecycle-only access; can reactivate with exact confirmation and fresh password | Cannot self-reactivate | Cannot self-reactivate | **PASS — automated.** `tests/test_owner_deactivation_api.py` and `tests/test_owner_reactivation_api.py`. |
| Feature-flag safeguard | Deactivation API remains disabled until authorized | Same | Same | Same | **PASS — config/test evidence.** `OWNER_DEACTIVATION_API_ENABLED=false`; no deployment or environment change is part of PR #72. |

## Appointment-to-notification integration evidence

The added test `tests/test_appointment_notification_lifecycle.py::test_committed_appointment_owner_notification_delivers_after_deactivation` exercises the following single flow:

1. Register an owner and create a service and business hours through the API.
2. Create a linked Telegram owner recipient and active platform bot fixture.
3. Create an appointment through `POST /appointments`.
4. Read the committed appointment, `TelegramOwnerNotification`, and `telegram.owner_appointment_alert` background job from PostgreSQL.
5. Transition the tenant from `active` to `deactivated` through `transition_tenant_lifecycle`.
6. Execute the real owner-notification worker with only the external Telegram provider call stubbed.
7. Verify the owner alert is sent, the notification ledger is `sent`, and the original appointment remains confirmed while the tenant remains deactivated.

This proves the REST appointment creation → durable owner notification/job → deactivation → worker delivery path. It does **not** prove customer-notification delivery or every Telegram/WhatsApp conversation-driven booking variant.


### Customer-notification integration evidence

The integration tests now cover three event paths:

- `test_customer_cancellation_reply_delivers_after_deactivation`: a committed Telegram cancellation is followed by the real Telegram outbound worker after deactivation.
- `test_customer_reschedule_reply_delivers_after_deactivation`: a committed Telegram reschedule is followed by the real Telegram outbound worker after deactivation; the appointment remains confirmed at its new start time.
- `test_whatsapp_booking_confirmation_delivers_after_deactivation`: the real WhatsApp conversation processor commits an appointment and preserved customer outbox record; after deactivation, the real WhatsApp outbound worker sends it through a stubbed provider boundary.

Each test checks the durable ledger state and tenant lifecycle state. These tests stub only external provider/provisioning calls, not the conversation processor, enqueue service, lifecycle transition service, or outbound worker.

## Cross-cutting acceptance gates

| Gate | Evidence | Status |
|---|---|---|
| Tenant-first lock order for final booking/retail writes | PostgreSQL blocking-PID concurrency tests in ADM-011 REST lifecycle tests | **PASS — automated** |
| Tenant-first lock order for Telegram/WhatsApp conversation workers | ADM-012/ADM-014 worker and lock-order tests | **PASS — automated** |
| Provider calls outside DB transaction/row locks | Worker implementations commit the `sending` transition before provider calls; worker tests cover persisted status changes | **Code-reviewed; retain as independent review checkpoint** |
| Worker commit-boundary race with actual writes | ADM-014 real WhatsApp processor/enqueue commit-boundary test | **PASS — automated** |
| Outbound idempotency includes delivery-preservation policy | WhatsApp and Telegram enqueue regression tests | **PASS — automated** |
| Queue retry/dead-letter and idempotency semantics | `tests/test_job_queue.py::test_telegram_lifecycle_job_retries_then_dead_letters_after_max_attempts`; `tests/test_telegram_lifecycle.py::test_inactive_telegram_event_job_completes_without_retry_or_dead_letter`; existing queue idempotency and channel-worker ambiguous-send tests | **PARTIAL; new tests pending CI.** Covers a Telegram inbound lifecycle job's retry/dead-letter transition and proves lifecycle-suppressed stale work completes without retry. Extend/confirm equivalent WhatsApp job behavior and review cross-channel idempotency/lease recovery before full acceptance.
| Tenant data and audit preservation | ADM-011 mutation snapshots and lifecycle service tests | **PASS — automated for tested records; not a deletion/retention certification** |
| Audit append-only governance and platform audit stream | Separate Issue #64 | **OUT OF SCOPE / NOT ACCEPTED by ADM-014/015** |
| Staging and production runtime acceptance | No deployment performed for this candidate | **NOT RUN** |
| Independent review of combined PR #72 | Awaiting reviewer inspection of final head and CI | **PENDING** |

## Closure decision

Do not close Issues #58, #59 or #60 solely because the combined CI passes. Before closure, record:

1. Independent review of the final combined PR #72 head.
2. Successful CI for that exact head.
3. Resolution or explicit acceptance of the partial rows above, especially actual Telegram ingress coverage for suspended/pending tenants, customer appointment-notification end-to-end delivery, and lifecycle-specific queue retry/dead-letter semantics.
4. A staging acceptance run after an explicitly authorized deployment, with the feature flag still disabled unless a separate activation decision is approved.
5. A final cross-channel acceptance record covering appointment/retail writes, Telegram/WhatsApp ingress and workers, outbound preservation/suppression, and owner lifecycle authentication.

No PR merge, deployment, feature-flag activation, or issue closure is authorized by this matrix.
