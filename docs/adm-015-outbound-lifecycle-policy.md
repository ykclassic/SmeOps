# ADM-015 — Outbound lifecycle policy

- Outbound records default to `deliver_after_deactivation = false`.
- Ordinary booking prompts and conversational follow-ups are suppressed after deactivation.
- Customer confirmations and appointment updates are marked deliverable only in the same transaction as a successfully persisted appointment creation, cancellation, or reschedule.
- Telegram owner appointment alerts are durable appointment-event records; they remain deliverable after voluntary deactivation because creation is coupled to the appointment transaction.
- Suspended and pending tenants never receive outbound delivery, including records marked deliverable after deactivation.
- Stale `sending` records become `unknown` and are never blindly retried.
- Lock order is Tenant → outbound ledger. Provider API calls occur only after commit, without database row locks.
- The worker's locked `queued → sending` transition is the authorization boundary. A queued record observing deactivation is sent only if its persisted appointment-lifecycle flag permits it.
- WhatsApp's 24-hour customer-service window, opt-out, connection, provider rejection, and unknown-outcome rules remain in force.
- Downgrade refuses to drop the policy columns while any preserved notification remains.
