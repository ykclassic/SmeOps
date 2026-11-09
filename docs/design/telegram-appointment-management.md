# Telegram Customer Appointment Management

## Status

Approved implementation design. This increment extends the existing shared Telegram platform bot and conversation engine. It does not introduce a second conversation engine or remove WhatsApp support.

## Goals

1. Notify customers when a business cancels or reschedules an appointment.
2. Let a customer verify their identity through Telegram contact sharing.
3. Let a verified customer list and manage only their own appointments.
4. Notify the business owner when a customer cancels or reschedules.
5. Preserve tenant isolation, durable outbound delivery, auditability, and duplicate-update safety.

## Customer identity binding

Add a tenant-scoped Telegram customer identity association containing:

- tenant ID and customer ID;
- platform bot ID;
- Telegram user ID and chat ID;
- verification timestamp and optional revocation timestamp.

Enforce database uniqueness so that, within a tenant, a Telegram user identity cannot be ambiguously bound to multiple customer records. The same Telegram user may have separate customer associations in different tenants.

Verification flow:

1. A customer enters a tenant-specific booking deep link and starts the shared bot.
2. The bot requests Telegram's native contact-share action.
3. Accept only a contact payload where the contact's `user_id` equals the sending Telegram message's `from.id`.
4. Normalize the shared phone number to E.164 and match it against an existing customer phone number in the selected tenant.
5. Bind the Telegram identity only after an exact tenant-scoped match.
6. Reject typed phone numbers as identity proof. Do not bind a contact shared by a different Telegram user.
7. If no customer matches, explain that the business must first create a booking/customer record; do not create a verified identity from an untrusted phone string.

Contact payloads must be handled as structured Telegram updates, not converted into ordinary message text. Confirm webhook update subscriptions include message/contact updates.

## Customer menu and state machine

Extend the existing conversation engine and its persisted conversation context. Keep the menu usable without AI classification.

Commands/menu actions:

- `/start`: show the business menu and identity-verification state.
- `/book`: start the existing booking flow.
- `/appointments`: show the verified customer's upcoming appointments with opaque callback identifiers.
- `/cancel`: select an eligible appointment, show details, require explicit confirmation, then cancel.
- `/reschedule`: select an eligible appointment, collect a new date/time using the existing availability and business-hours rules, show the proposed change, and require explicit confirmation.
- `/help`: explain available actions.

Use inline keyboard callbacks where practical. Callback data must be compact, non-sensitive, and treated as untrusted input. Resolve every callback against the authenticated Telegram sender, tenant context, verified customer identity, and current appointment state on the server.

Do not trust appointment IDs, tenant IDs, customer IDs, or proposed appointment state supplied by callback data. Query appointments by tenant and verified customer association. Lock the appointment row before mutation. A customer can only cancel or reschedule their own eligible confirmed appointment.

## Business-triggered notifications

For business-side cancellation and rescheduling:

- Enqueue the customer Telegram message in the same database transaction as the appointment state change and audit event.
- Include business name, appointment reference, service, and resulting date/time. For reschedules, include previous and new schedule.
- If no verified Telegram identity exists, do not guess a chat recipient; record an explicit not-deliverable outcome or skip with an observable reason.
- Notification delivery failure must not undo an already committed appointment mutation.
- Use stable idempotency keys derived from the lifecycle event so transaction retries cannot create duplicate messages.
- Reuse the durable Telegram outbound queue and existing ambiguous-send handling. Never blindly retry an outcome that may already have been accepted by Telegram.

## Owner notifications

When a customer cancels or reschedules, enqueue an owner alert through the existing owner-notification mechanism. Include appointment reference, customer display name where appropriate, and the resulting status/schedule. Use an event-derived idempotency key.

## Audit and transaction boundaries

- Record actor type and relevant lifecycle event in the existing audit-event mechanism. Customer-initiated actions must be distinguishable from owner/business actions.
- Appointment mutation, audit event, customer notification enqueue, and owner notification enqueue must commit atomically.
- Telegram network delivery occurs asynchronously after commit.
- Repeated webhook update IDs must remain deduplicated by the existing webhook-event mechanism.
- Repeated callbacks or confirmation messages must be idempotent and must not apply a second state transition.

## Authorization and privacy

- No cross-tenant lookups or mutations.
- No customer appointment access before contact verification.
- Contact verification requires sender/contact user ID equality.
- Never expose another customer's phone number or appointment details.
- Expire or clear stale pending customer-management state after a bounded period.
- Re-check appointment ownership, status, and slot availability at confirmation time.
- Avoid logging contact phone numbers, callback payload secrets, bot tokens, or message contents containing personal data.

## Tests and acceptance gates

### Identity
- Valid self-shared contact binds to the matching customer in the active tenant.
- Contact user ID mismatch is rejected.
- Typed phone number does not verify identity.
- Unknown phone does not create a verified identity.
- Tenant A identity cannot access Tenant B appointments.
- Duplicate identity binding is rejected or safely idempotent.

### Business notifications
- Business cancellation queues one customer notification in the same transaction.
- Business reschedule queues one customer notification with old and new times.
- No verified identity produces a clear non-deliverable outcome without leaking appointment data.
- Transaction rollback leaves no orphaned notification.
- Replayed lifecycle operation does not enqueue duplicates.

### Customer actions
- Verified customer sees only their own upcoming appointments.
- Unverified user cannot list, cancel, or reschedule.
- Customer can cancel a confirmed appointment after explicit confirmation.
- Customer can reschedule to a valid available slot after explicit confirmation.
- Conflicts, past dates, closed hours, and occupied slots are rejected.
- A different customer's appointment and cross-tenant appointment IDs are inaccessible.
- Duplicate callback/update delivery does not repeat the mutation.

### Owner notifications and delivery
- Customer cancellation/reschedule generates one owner alert.
- Outbound worker preserves queued/sent/failed/unknown semantics.
- Ambiguous Telegram send is not blindly retried.

### Delivery
1. Add regression tests before implementation.
2. Implement business-triggered lifecycle notifications.
3. Add the identity model and Alembic migration.
4. Implement contact verification and webhook parsing.
5. Add appointment listing, cancellation, and rescheduling to the existing conversation engine.
6. Add owner alerts for customer lifecycle events.
7. Run full test suite, lint/type checks, and migration checks.
8. Open a PR; do not merge until required checks pass and review is complete.
9. Apply the migration to staging using the approved migration workflow.
10. Run real Telegram acceptance tests and inspect durable outbound/owner notification records.

## Out of scope

- Replacing the shared platform bot.
- Creating a parallel conversation engine.
- Removing WhatsApp support.
- Enabling Discord.
- Rotating the previously exposed Telegram token in this increment; that remains a separately tracked security-audit action. The token remains compromised until rotated/revoked.
