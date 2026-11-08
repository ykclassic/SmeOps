# Telegram platform-bot onboarding architecture

Status: proposed implementation contract  
Owner: SmeOps engineering  
Baseline: `main` at `a16a23d92f1e07565408435b8a58568bab4bf1a6`

## Goal

A newly registered business owner must be able to open **Dashboard → Settings → Telegram → Connect Telegram**, press Start in the existing `@smeops_bot`, and receive appointment alerts. Customers must be able to open a tenant-specific deep link to book that business. Business owners must never create a bot, provide a BotFather token, or use Swagger.

The platform operates one centrally managed Telegram bot. Tenant identity is resolved by SmeOps from an opaque business booking reference or a one-time owner-link invitation; it is never accepted from an untrusted tenant ID in an arbitrary message.

## Audit findings on the current main branch

1. `TelegramConnection` is tenant-scoped and has a unique constraint on `tenant_id`; it stores one bot token and webhook secret per tenant. This is a one-bot-per-tenant architecture, not a shared platform bot.
2. `POST /telegram/connection` requires an owner to submit a BotFather token and calls `getMe` and `setWebhook`. This is the current Swagger-based onboarding blocker.
3. `POST /telegram/owner-link` requires an active tenant `TelegramConnection` to construct the bot deep link.
4. The inbound webhook resolves a tenant by looking up the bot ID in `telegram_connections` before persisting the update. That cannot route one platform bot to many tenants.
5. `TelegramWebhookEvent` requires `tenant_id` and deduplicates update IDs per tenant. A platform webhook must deduplicate globally by (platform bot ID, Telegram update ID), before a tenant may be known.
6. `_consume_owner_link` only searches owner-link records within the tenant already selected by the webhook. The platform webhook must resolve and consume owner invitations globally, then bind the authenticated invitation to its tenant and owner.
7. Owner-alert delivery looks up a tenant's `TelegramConnection` and decrypts that tenant's bot token. It must instead use the single platform credential while keeping recipient lookup and appointment data tenant-scoped.
8. The Next.js API proxy has an explicit route allowlist and currently excludes Telegram connection and owner-link routes. Browser calls cannot reach these APIs through the secure same-origin session proxy.
9. The dashboard tab type and rendered navigation currently include WhatsApp but no Telegram integration screen.
10. The existing owner-link and notification tables already use tenant/owner-scoped constraints. Preserve these boundaries; do not weaken them to make the platform bot work.

## Target architecture

### Platform bot credential and webhook

- Introduce one singleton `TelegramPlatformBot` record (or equivalent platform configuration) containing bot ID, username, encrypted bot token, webhook-secret hash, status, and timestamps.
- Promote the currently provisioned `@smeops_bot` credential into this singleton during migration, without exposing or re-entering the token. Verify there is exactly one eligible active legacy connection for the expected platform bot before promotion. Abort migration or use an explicit operator preflight if this invariant is not true; never silently select an arbitrary tenant's bot.
- Register one webhook at `/webhooks/telegram/platform`, authenticated with Telegram's `X-Telegram-Bot-Api-Secret-Token` and a secret derived from the existing application secret and platform bot identity.
- Keep the existing legacy per-tenant connection tables during a compatibility window. Do not drop or rewrite customer history.
- The platform bot token is an SmeOps operator credential, not a business credential. It remains encrypted at rest and only the worker/integration adapter can decrypt it.

### Inbound update routing

- Persist the raw update encrypted before acknowledging it. Deduplicate globally on (platform_bot_id, provider_update_id); a duplicate update must enqueue no second processing job.
- Route `/start <payload>` first. Payloads are opaque, random, URL-safe values with a database hash; do not encode raw tenant IDs or owner IDs in Telegram deep links.
- Owner invitations resolve against a globally unique invitation hash, expire quickly, are single-use, and are consumed only from a private chat where Telegram sender ID equals chat ID. The resulting owner link is still constrained by (tenant_id, owner_user_id) and active owner status.
- Customer booking links resolve to an active tenant through a separate persistent booking-link token/hash. A customer deep link selects that tenant's current bot-chat context.
- For ordinary subsequent messages, resolve the Telegram private chat to its currently selected tenant context. A user can switch business context by opening another business's deep link. The UX must clearly show the selected business before collecting personal details or creating an appointment.
- If no valid tenant context exists, do not guess. Ask the user to open a business-specific booking link.
- Ignore group chats, bot-authored messages, unsupported update types and malformed start payloads safely.
- Store tenant-scoped conversations and messages using composite tenant foreign keys. Never query a conversation, booking, owner link or service without the resolved tenant predicate.

### Booking links

- Each tenant receives a stable, opaque, non-sequential booking reference generated with a cryptographically secure random source.
- Public URL format: `https://t.me/smeops_bot?start=<opaque_booking_reference>`.
- A public booking reference identifies the business only; it grants no owner access and must not be reusable as an owner-link token.
- Owner links remain short-lived, one-time, independently hashed invitations.
- Dashboard supports copy/share and regeneration/revocation. Regeneration invalidates the previous reference and should be audited.
- Customer journey begins with a business-branded confirmation prompt, then calls the existing deterministic shared booking engine.

### Owner notifications

- Owner account linking and appointment alert recipient records remain tenant-scoped.
- Alert worker loads the singleton platform credential, then sends to the linked owner's chat ID.
- Appointment and owner recipient are loaded with tenant predicates and verified against the same tenant.
- Notification row and durable queue job are inserted in the appointment transaction. Delivery failure must never roll back a confirmed appointment.
- Keep the existing uniqueness key per tenant, owner and appointment. A duplicate booking event or worker retry must not produce a second alert.
- Record provider message ID and terminal delivery state; do not blindly retry ambiguous send outcomes.
- Support one Telegram account linked to multiple tenants (one owner-link row per tenant/owner); do not conflate tenant preferences or notifications.

## Migration and compatibility

1. Add platform-bot credential, tenant booking-link and platform webhook-event/session persistence in a forward-only migration.
2. Promote the currently active legacy `@smeops_bot` token into the platform singleton, preserving encryption and validating bot identity through Telegram `getMe` during a controlled setup/acceptance step. Migration itself must not call external network APIs.
3. Create a unique booking reference for every existing active tenant; return it through the owner dashboard.
4. Update webhook ingress, workers, outbound adapter and owner notification worker to use the singleton platform credential and resolved tenant context.
5. Keep legacy `/telegram/connection` endpoints disabled or return a clear deprecation response after the dashboard switches. Do not accept per-tenant bot tokens for the shared-bot product flow.
6. Do not delete legacy encrypted bot tokens until the new webhook and notification path is live, verified, and a separate cleanup migration has been reviewed.

## Dashboard and same-origin API

- Add Telegram to the existing dashboard navigation/settings experience.
- Use the existing HttpOnly session cookie and Next.js same-origin API proxy; never expose backend bearer tokens to browser JavaScript.
- Add exact allowlist entries for owner-link create/status/disconnect and booking-link read/regenerate routes.
- UI states: Not connected, Linking, Connected, Delivery issue. Provide a Connect button, Telegram deep link, copy booking link, link status, last connection timestamp, and disconnect/reconnect actions.
- Show the business booking link independently from owner notification linking: they are separate controls and separate tokens.
- No bot token, app secret or webhook secret appears in frontend state, API response, logs or browser storage.

## Required tests before merge

- Migration from the actual current schema, including promotion preconditions and no token disclosure.
- Two independent tenants use the same platform bot and distinct booking links.
- Customer A opening tenant A link is routed to A; customer B opening tenant B link is routed to B.
- A customer's selected tenant context switches only after a valid other business link.
- Missing, expired, revoked and malformed booking references are rejected without falling back to another tenant.
- Owner invitations are one-time, expiring, private-chat-only, sender/chat identity-bound, and cannot be used by another tenant.
- Owner A cannot query or receive Tenant B appointment/customer data.
- Same update ID delivered twice creates one webhook event and one processing job.
- Same appointment event/retry creates one notification per linked owner and tenant.
- Worker sends alerts for at least two tenants using the same platform bot token and correct chat IDs.
- Provider rejection and ambiguous send outcomes do not cause duplicate messages.
- Legacy per-tenant provisioning cannot accidentally reconfigure the platform webhook.
- Dashboard integration works through the same-origin proxy with the HttpOnly session; no Swagger use.
- Full backend migration/lint/test suite, frontend type-check/build and browser-level onboarding test pass.

## Deployment and acceptance gates

- Do not merge until required CI checks pass against the exact PR head SHA.
- Apply the database migration manually through the existing staging migration workflow after merge approval; verify Alembic head and metadata drift.
- Configure/promote the existing platform bot credential without asking business owners for a token. Never request secrets in chat.
- Verify Render health, webhook registration and worker processing.
- Complete two-business live acceptance: owner A and owner B link independently; each business shares its own booking link; create one appointment in each; verify each owner receives only their own reference and booking details; replay a webhook and verify no duplicate appointment/alert.
- Vercel rate limits: avoid repeated preview deployments. Run frontend type-check/build in CI and perform one consolidated deployment after CI and backend staging acceptance. Do not claim live frontend acceptance until a READY deployment is confirmed.

## Non-goals

- Discord integration.
- A separate bot per tenant.
- Customer or owner Telegram bot tokens.
- Cross-tenant customer identity linking.
- Changing the WhatsApp channel or its provider-specific policies.
