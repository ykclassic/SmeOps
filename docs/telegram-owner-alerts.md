# Telegram owner appointment alerts

SmeOps uses the tenant's existing Telegram bot for customer conversations and owner alerts.

## Connect an owner account

Authenticate as a tenant owner and call `POST /telegram/owner-link`. The API returns a private Telegram deep link containing a random, single-use invitation token. The invitation expires after 15 minutes. Open it in Telegram while logged into the Telegram account that should receive alerts.

Check `GET /telegram/owner-link` until it reports `{"linked": true, "status": "active"}`. The bot consumes the token only from a private, human-authored Telegram message. Tokens are stored as SHA-256 hashes and are never logged or returned again.

Disconnect using `DELETE /telegram/owner-link`. A disconnected account stops receiving future alerts.

## Alert behavior

A confirmed appointment creates one tenant-scoped notification record and durable worker job per linked active owner in the same database transaction as the booking. The worker sends the alert through the tenant's existing active Telegram bot. Notifications contain business, reference, service, customer name, local appointment date/time, and status; customer phone numbers are excluded.

Delivery states are `queued`, `sending`, `sent`, `failed`, and `unknown`. A worker restart after the provider request began moves a still-`sending` notification to `unknown` instead of blindly resending, because Telegram Bot API sends do not provide an idempotency key. A definite provider rejection is marked failed. An alert failure does not change the appointment's confirmed status.

## Required migration

Apply Alembic revision `0014_telegram_owner_alerts` to the intended staging database before using these endpoints or booking new appointments for owner alerts.
