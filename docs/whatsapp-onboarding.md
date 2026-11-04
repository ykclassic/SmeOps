# WhatsApp Embedded Signup v4

SmeOps uses Meta's current Embedded Signup v4 flow. Embedded Signup v2/v3 were retired on 15 October 2026; do not configure a legacy v2/v3 flow.

## Meta prerequisites

Before real customer onboarding is enabled, the SmeOps operator must:

1. Own and verify the Meta Business Portfolio that owns the SmeOps Meta app.
2. Be approved as a Meta Tech Provider or Solution Partner for the intended onboarding model.
3. Add WhatsApp Cloud API to the Meta app and enable Facebook Login for Business.
4. Create an Embedded Signup v4 configuration in Meta App Dashboard, using WhatsApp Embedded Signup and the system-user token option.
5. Request and receive Advanced Access/App Review for the required WhatsApp permissions, including `whatsapp_business_management` and `whatsapp_business_messaging`, and `business_management` where required by the selected Tech Provider flow.
6. Configure the SmeOps production HTTPS domain in the Facebook Login for Business allowed domains and OAuth redirect settings.
7. Configure the Meta webhook callback to the SmeOps webhook URL and subscribe to the WhatsApp account and message events required by the application.
8. Complete Meta's business verification, app review and partner requirements before onboarding external customer businesses.

Meta's official references:
- [Embedded Signup implementation](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/implementation)
- [Embedded Signup v4](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/version-4)
- [Embedded Signup onboarding for Tech Providers](https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/onboarding-customers-as-a-tech-provider)
- [Meta WhatsApp Business Platform API collection](https://www.postman.com/meta/whatsapp-business-platform/)

## Required environment variables

Backend (Render API service):

- `META_APP_ID` — Meta application ID.
- `META_APP_SECRET` — Meta application secret. Secret; never expose to the browser.
- `WHATSAPP_GRAPH_API_VERSION` — pinned Graph API version, format `vNN.N`.
- `WHATSAPP_TOKEN_ENCRYPTION_KEY` — Fernet key used to encrypt customer business tokens and two-step PINs.

Frontend (Vercel or other Next.js host):

- `SMEOPS_API_BASE_URL` — server-side FastAPI origin.
- `NEXT_PUBLIC_META_APP_ID` — same Meta app ID; public identifier.
- `NEXT_PUBLIC_META_EMBEDDED_SIGNUP_CONFIG_ID` — v4 configuration ID from Meta.
- `NEXT_PUBLIC_META_GRAPH_API_VERSION` — same pinned Graph API version used by the backend.
- `NEXT_PUBLIC_META_SOLUTION_ID` — optional Partner Solution ID if Meta requires it for the configured partner flow.

Do not set `META_APP_SECRET`, WhatsApp access tokens, or encryption keys as `NEXT_PUBLIC_*` variables.

## Customer onboarding flow

1. The owner enters a six-digit WhatsApp two-step verification PIN in the dashboard.
2. The browser launches Facebook Login for Business using the v4 configuration ID.
3. The browser receives the exchangeable authorization code from the FB.login callback and receives WABA/phone-number IDs through the `WA_EMBEDDED_SIGNUP` session message. These are separate channels and may arrive in either order.
4. The browser sends the code and IDs to the authenticated owner-only SmeOps endpoint. The code is never exchanged in the browser.
5. The backend exchanges the short-lived code with Meta, encrypts the returned business integration token and PIN, persists the connection as `pending`, and enqueues a durable onboarding job.
6. The worker verifies the selected phone number belongs to the selected WABA, subscribes the SmeOps app to WABA webhooks, and registers the number with the submitted two-step PIN.
7. The dashboard polls the connection status. Only after all Graph API steps succeed does the worker mark the connection `active`.
8. If a post-exchange step fails, the worker retries the durable job up to its configured attempt limit. The connection remains `pending` with a sanitized error code if the job exhausts its attempts. The owner can enqueue a retry without exposing or re-entering the business token.

## Security and operational controls

- Only a tenant owner can start or retry Embedded Signup.
- Tenant ID comes from the authenticated owner; it is never accepted from the browser.
- The authorization code, Meta app secret, access token and two-step PIN are not logged or returned in API responses.
- The business access token is encrypted at rest. The two-step PIN is encrypted only while onboarding is pending and is erased immediately after Meta confirms number registration.
- The backend verifies the phone-number/WABA relationship before registration.
- A phone number cannot be connected to more than one SmeOps tenant.
- The owner can disable the SmeOps connection. This stops SmeOps processing and outbound sends but does not revoke the Meta business integration token or remove the app from the WABA; remove the SmeOps app in Meta Business settings to revoke authorization fully.
- Outbound messaging remains disabled until connection status becomes `active`.
- The token expiry timestamp is stored when Meta returns `expires_in`; automatic token refresh/rotation is not yet implemented.
- Authorization-code exchange is synchronous and bounded to an 8-second Meta request timeout. Subsequent verification, subscription and number registration run in the durable worker so the dashboard request does not wait on multiple Graph API calls.
- The Next.js BFF route declares a 15-second maximum function duration. The selected frontend host/plan must support at least this duration for the authorization-code exchange.
- Coexistence onboarding, multiple phone numbers per tenant, partner credit-line management and Meta template management are not implemented in this pilot.
- Meta may require payment method attachment for a Tech Provider customer before messaging can be used. The dashboard must not report successful delivery until live tests confirm it.

## Live acceptance

CI mocks Meta Graph API calls. It does not prove Meta app review, partner approval, customer authorization, WABA subscription, number registration, webhook delivery or real message delivery. Those require the real Meta app and a designated test business account.
