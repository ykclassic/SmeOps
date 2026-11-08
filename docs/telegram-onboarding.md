# Telegram bot onboarding and messaging

This integration provisions one Telegram bot per SmeOps tenant. Bot credentials are
verified with Telegram's `getMe` method, encrypted at rest, and never returned by
the API. Webhook registration uses Telegram's secret-token header.

## Required API configuration

Set these values in the API service environment before provisioning a bot:

- `PUBLIC_API_BASE_URL`: public HTTPS API origin, with no trailing slash. For the
  current staging service this is `https://smeops-api-staging.onrender.com`.
- `WHATSAPP_TOKEN_ENCRYPTION_KEY`: existing Fernet key used to encrypt channel
  access tokens and payloads. Do not rotate it without a migration plan for all
  encrypted channel credentials.
- `APP_SECRET_KEY`: existing application secret. Telegram webhook secrets are
  deterministically derived from this key and the Telegram bot ID, then stored
  only as SHA-256 hashes.

Do not put bot tokens in source control, issue comments, logs, or chat messages.
Enter them only through the authenticated API over HTTPS.

## Provision a bot

1. Create a bot with Telegram's official BotFather and copy its token.
2. Authenticate as the tenant owner.
3. Call `POST /telegram/connection` with a bearer access token:

```http
POST /telegram/connection
Authorization: Bearer <OWNER_ACCESS_TOKEN>
Content-Type: application/json

{
  "bot_token": "<BOT_TOKEN>"
}
```

The API verifies the token with `getMe`, persists the encrypted token in a
`pending` connection, and calls Telegram `setWebhook` with:

```text
<PUBLIC_API_BASE_URL>/webhooks/telegram/<BOT_ID>
```

The webhook secret is not returned to the client. A successful registration
returns `status: active`. If Telegram cannot confirm registration, the API
returns `status: pending`; retry with the owner-authenticated
`POST /telegram/connection/complete` endpoint.

Use `GET /telegram/connection` to inspect the connection status. The response
contains only bot ID, username, and status.

## Inbound webhook

Telegram sends updates to `POST /webhooks/telegram/{bot_id}`. The API checks
`X-Telegram-Bot-Api-Secret-Token`, enforces a 1 MB body limit, validates the
update ID, encrypts and persists the event, and queues
`telegram.accept_inbound`. Duplicate update IDs are acknowledged without
creating duplicate jobs.

Inbound processing currently persists Telegram conversations and encrypted
message history. It is deliberately not connected to the appointment-booking
conversation engine yet.

## Queue an outbound message

The owner-authenticated `POST /telegram/messages` endpoint queues a message
for a tenant-owned Telegram conversation:

```http
POST /telegram/messages
Authorization: Bearer <OWNER_ACCESS_TOKEN>
Content-Type: application/json

{
  "conversation_id": "<TELEGRAM_CONVERSATION_UUID>",
  "body": "Your message",
  "idempotency_key": "<NEW_UUID>"
}
```

The message, conversation history, audit event, and
`telegram.send_text` background job are committed together. The worker marks
the ledger `sent`, `failed`, or `unknown`. An ambiguous provider outcome is
never blindly resent: a worker restart while a record is `sending` changes it
to `unknown` to avoid duplicate customer messages.

## Operational boundaries

- Telegram bot provisioning is API-only; the owner dashboard does not yet have
  a Telegram setup screen.
- One bot is supported per tenant.
- This increment does not implement appointment booking, phone collection,
  staff inbox actions, bot disconnection, or cross-channel identity linking.
- A `sent` state means Telegram returned a message ID; it is not a recipient
  read receipt.
- The Render staging API uses a Free service and may sleep. A sleeping service
  can delay webhook handling; do not treat this as production-grade availability.
