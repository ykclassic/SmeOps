# WhatsApp outbound delivery lifecycle

This increment adds the first outbound Cloud API path and an authenticated owner/manager enqueue endpoint. It is not yet an AI customer-assistant workflow.

## Flow

1. An authenticated owner/manager calls `POST /whatsapp/messages`, or an internal application service calls `enqueue_whatsapp_text`, with a recipient, message body and caller-generated idempotency key. The route derives tenant identity from the authenticated user.
2. The service encrypts the recipient and body, creates a durable outbound ledger row and enqueues a `whatsapp.send_text` job in the same database transaction.
3. The worker marks the ledger row `sending` before making the external HTTP request.
4. A confirmed Meta response stores the provider message ID and changes the state to `sent`.
5. Signed WhatsApp status webhooks are persisted by the existing webhook endpoint and correlated by provider message ID. `sent`, `delivered`, `read` and `failed` statuses update the ledger.
6. Delivery states are monotonic; a late `sent` event cannot downgrade `delivered` or `read`.

## Retry safety

Meta message creation is an external write without a provider idempotency guarantee in this adapter. The outbound job allows at most two attempts: the first may perform the provider write, while the second only resolves a stale `sending` state to `unknown`.

- A definitive HTTP 4xx response is recorded as `failed`.
- A timeout, transport failure, 5xx response or malformed success response is recorded as `unknown`.
- If a worker restarts after marking a message `sending`, the recovery attempt marks it `unknown` rather than sending again.
- `unknown` must be reconciled against provider status events or operational evidence. It must not be blindly retried.

This deliberately favours avoiding duplicate customer messages over automatic recovery from an ambiguous network outcome.

## Required configuration

- `WHATSAPP_TOKEN_ENCRYPTION_KEY`: valid Fernet key used to encrypt connection tokens and outbound recipient/body values.
- `WHATSAPP_GRAPH_API_VERSION`: explicit Meta Graph API version in `vNN.N` form. The sender fails closed if it is not configured.
- Existing connection settings: `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, and platform-admin provisioning credentials.

Do not put real credentials in source control or test fixtures. Inject production values through the deployment secret manager.

## Current limitations

- The send endpoint is intended for authenticated owner/manager operational use and controlled integration testing; customer conversation orchestration and AI-generated replies are not implemented.
- Inbound customer messages are still persisted and acknowledged by the worker; conversation orchestration and AI replies are not implemented.
- Meta Embedded Signup, token refresh, connection health checks, templates, media messages, opt-out management and a tenant-facing message history UI remain future work.
- Delivery status webhooks can arrive before the sender commits its provider message ID. The event job retries with bounded backoff and then dead-letters; dead-letter reconciliation is still an operational follow-up.
- Text sends are permitted only when the tenant has a persisted inbound conversation for that recipient and the latest customer message is less than 24 hours old. The policy is checked both when a send is queued and immediately before the provider call. Approved templates outside the 24-hour window are not implemented; text sends outside the window are rejected.
- This increment has automated adapter and database tests, but no live Meta account or real phone-number delivery verification is claimed.


## Inbound conversation persistence (draft increment)

Verified inbound webhook events of type `message.received` are now processed into a tenant-scoped conversation inbox.

- Conversation lookup uses an HMAC-SHA256 of the normalized sender number with `APP_SECRET_KEY`; the sender number itself is Fernet-encrypted. Keep this key stable or perform a controlled contact-hash migration when rotating it, otherwise existing conversations will no longer match outbound recipient lookups.
- Provider message IDs are unique per tenant, so worker retries do not create duplicate message history.
- Text message bodies are Fernet-encrypted. Non-text message types are retained as metadata without attempting to download or interpret media.
- Provider timestamps are stored in UTC. Invalid or absent timestamps fall back to the worker receipt time for conversation ordering while the original timestamp remains null.
- Webhook events are marked processed only after the conversation/message write succeeds.
- The worker does not yet send an automatic response, invoke an LLM, create bookings, or expose a tenant dashboard. Those are separate next increments.


## Messaging policy enforcement (draft increment)

- Outbound text requires a tenant-scoped conversation opened by an inbound customer message within the preceding 24 hours.
- The same rule is rechecked by the worker immediately before making the external provider request, so queue delay cannot silently extend the service window.
- Inbound `STOP`, `UNSUBSCRIBE`, `END` or `QUIT` commands persist an opt-out state. Outbound text is rejected for opted-out contacts.
- Opt-out is sticky in this increment. There is no self-service re-subscription flow or owner override yet.
- Approved WhatsApp message templates for messages outside the customer-service window are not implemented. The system fails closed instead of sending free-form text.
- The API rejects an expired-window request with HTTP 409 and an opted-out recipient with HTTP 403. Worker-side policy failures are marked failed with safe internal error codes.
