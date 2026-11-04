# WhatsApp outbound delivery lifecycle

This increment adds the first outbound Cloud API path. It is a backend capability, not yet a customer-facing assistant workflow.

## Flow

1. An application service calls `enqueue_whatsapp_text` with the authenticated tenant ID, E.164-style recipient, message body and caller-generated idempotency key.
2. The service encrypts the recipient and body, creates a durable outbound ledger row and enqueues a `whatsapp.send_text` job in the same database transaction.
3. The worker marks the ledger row `sending` before making the external HTTP request.
4. A confirmed Meta response stores the provider message ID and changes the state to `sent`.
5. Signed WhatsApp status webhooks are persisted by the existing webhook endpoint and correlated by provider message ID. `sent`, `delivered`, `read` and `failed` statuses update the ledger.
6. Delivery states are monotonic; a late `sent` event cannot downgrade `delivered` or `read`.

## Retry safety

Meta message creation is an external write without a provider idempotency guarantee in this adapter. The outbound job is therefore enqueued with one attempt.

- A definitive HTTP 4xx response is recorded as `failed`.
- A timeout, transport failure, 5xx response or malformed success response is recorded as `unknown`.
- If a worker restarts after marking a message `sending`, a subsequent attempt marks it `unknown` rather than sending again.
- `unknown` must be reconciled against provider status events or operational evidence. It must not be blindly retried.

This deliberately favours avoiding duplicate customer messages over automatic recovery from an ambiguous network outcome.

## Required configuration

- `WHATSAPP_TOKEN_ENCRYPTION_KEY`: valid Fernet key used to encrypt connection tokens and outbound recipient/body values.
- `WHATSAPP_GRAPH_API_VERSION`: explicit Meta Graph API version in `vNN.N` form. The sender fails closed if it is not configured.
- Existing connection settings: `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, and platform-admin provisioning credentials.

Do not put real credentials in source control or test fixtures. Inject production values through the deployment secret manager.

## Current limitations

- The application service is internal; no customer-facing send API is exposed in this increment.
- Inbound customer messages are still persisted and acknowledged by the worker; conversation orchestration and AI replies are not implemented.
- Meta Embedded Signup, token refresh, connection health checks, templates, media messages, opt-out management and a tenant-facing message history UI remain future work.
- Delivery status webhooks can arrive before the sender commits its provider message ID. Such events remain retryable and require the worker retry policy to complete correlation.
- This increment has automated adapter and database tests, but no live Meta account or real phone-number delivery verification is claimed.
