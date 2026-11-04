# WhatsApp conversation inbox API

The authenticated owner/manager API exposes tenant-scoped conversation and message history for the dashboard.

## List conversations

`GET /whatsapp/conversations?limit=50&offset=0`

- Requires a bearer access token for an owner or manager.
- Returns conversations belonging only to the authenticated user's tenant.
- `limit` is between 1 and 100; `offset` must be non-negative.
- Customer phone numbers are decrypted only for the authenticated tenant's response.

## Read conversation messages

`GET /whatsapp/conversations/{conversation_id}/messages?limit=50&offset=0`

- Requires the same owner/manager role.
- Checks conversation ownership before querying messages.
- Returns 404 for a conversation outside the authenticated tenant, avoiding cross-tenant existence disclosure.
- Decrypts message bodies only for the authorized tenant response.
- Orders messages newest-first and supports bounded offset pagination.

## Security notes

- Tenant scope always comes from the authenticated user, never from a query parameter or request body.
- Encrypted fields, HMAC contact hashes, provider message IDs and access tokens are not returned.
- Message body and customer phone are personal data; frontend code must not persist them in browser logs or analytics.
- These read endpoints do not yet provide search, assignment, mark-as-read, export, retention/deletion, or dashboard UI.
