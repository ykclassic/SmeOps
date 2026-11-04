# Production deployment and acceptance runbook

This runbook describes the production deployment path for the SmeOps salon pilot. A green CI pipeline is necessary but does not prove live WhatsApp delivery or production acceptance.

## 1. Required accounts and infrastructure

- Render account for the FastAPI API and background worker.
- Managed PostgreSQL 16 database with backups and point-in-time recovery appropriate to the selected plan.
- Vercel project (or equivalent) for the Next.js dashboard.
- Meta Business Portfolio and WhatsApp Cloud API app approved for the Embedded Signup v4 Tech Provider/Solution Partner flow.
- A dedicated WhatsApp test business number and a separate test customer number.
- Optional OpenAI API project if AI intent classification is enabled.

## 2. Provision services

1. Create a managed PostgreSQL database. Use a private/internal connection string where available.
2. Create the Render API and worker services from `render.yaml`.
3. Configure the same production `DATABASE_URL` for API and worker.
4. Keep automatic deploy disabled until the production branch has passed the required CI checks.
5. Deploy the API first. Its pre-deploy command runs `alembic upgrade head`.
6. Verify `GET /health/live` and `GET /health/ready`.
7. Deploy the worker separately.
8. Verify `GET /health/worker` returns 200 with a fresh heartbeat.
9. Configure the Vercel project with `SMEOPS_API_BASE_URL` pointing to the API origin.
10. Configure the frontend Meta public variables and deploy the dashboard.

Do not run Alembic migrations independently from multiple service instances. The API pre-deploy command is the single migration runner in the supplied Render blueprint.

## 3. Configure secrets and variables

### Render API

| Variable | Required | Notes |
|---|---:|---|
| `APP_ENV` | Yes | Must be `production`. |
| `APP_SECRET_KEY` | Yes | Unique random secret, at least 32 characters. |
| `DATABASE_URL` | Yes | Managed PostgreSQL URL; use the private URL where available. |
| `WHATSAPP_VERIFY_TOKEN` | Yes | Random verification string configured in Meta webhook settings. |
| `WHATSAPP_APP_SECRET` | Yes | Meta app secret used for webhook signature validation. |
| `WHATSAPP_TOKEN_ENCRYPTION_KEY` | Yes | Fernet key; back it up separately. Losing it makes encrypted customer tokens and data unrecoverable. |
| `WHATSAPP_GRAPH_API_VERSION` | Yes | Pinned Meta Graph API version in `vNN.N` format. |
| `META_APP_ID` | Yes | Numeric Meta app ID. |
| `META_APP_SECRET` | Yes | Meta app secret; server-side only. |
| `PLATFORM_ADMIN_KEY` | Yes | High-entropy secret used for protected platform operations and the legacy internal provisioning endpoint. Configure the same value for API and worker services; never expose it to the dashboard. |
| `OPENAI_API_KEY` | Optional | Enables model-assisted intent classification. |
| `OPENAI_MODEL` | With API key | Model name selected for the SmeOps project. |

### Render worker

Use the same `DATABASE_URL`, `APP_SECRET_KEY` and `WHATSAPP_TOKEN_ENCRYPTION_KEY` as the API. Also configure `WHATSAPP_GRAPH_API_VERSION`, `OPENAI_API_KEY` and `OPENAI_MODEL` when AI classification is enabled.

### Vercel frontend

| Variable | Required | Notes |
|---|---:|---|
| `SMEOPS_API_BASE_URL` | Yes | Server-side API origin; never expose as `NEXT_PUBLIC_*`. |
| `NEXT_PUBLIC_META_APP_ID` | Yes for onboarding | Public Meta app ID. |
| `NEXT_PUBLIC_META_EMBEDDED_SIGNUP_CONFIG_ID` | Yes for onboarding | Embedded Signup v4 config ID. |
| `NEXT_PUBLIC_META_GRAPH_API_VERSION` | Yes for onboarding | Must match backend Graph API version. |
| `NEXT_PUBLIC_META_SOLUTION_ID` | If required | Partner solution ID from Meta. |

Never put `META_APP_SECRET`, Meta access tokens, `APP_SECRET_KEY`, or encryption keys in frontend variables. Do not paste test account passwords or API keys into source code or pull requests.

## 4. Meta setup

Complete the prerequisites in [docs/whatsapp-onboarding.md](whatsapp-onboarding.md). In particular:

- Confirm the Meta app is approved for the required WhatsApp permissions.
- Create and configure an Embedded Signup v4 configuration.
- Add the production dashboard domain to Facebook Login for Business allowed domains.
- Configure the webhook callback URL `https://YOUR_API_HOST/webhooks/whatsapp`.
- Set the same verification token in Meta and `WHATSAPP_VERIFY_TOKEN`.
- Subscribe the app to the WhatsApp account and message webhook fields.
- Use a dedicated test WABA and number before any external customer onboarding.

The SmeOps API persists the Meta token encrypted and does not return it. Confirm that the connected account appears as `active` in the dashboard. A `pending` state must be resolved before outbound sends are allowed.

## 5. Test account secrets for end-to-end acceptance

When the test account is ready, store its credentials in GitHub Actions repository/environment secrets or the deployment secret manager. Do not send the password in chat.

Recommended E2E variables:

- `E2E_BASE_URL` — deployed API origin.
- `E2E_DASHBOARD_URL` — deployed dashboard origin.
- `E2E_TEST_TENANT_SLUG` — dedicated test workspace.
- `E2E_TEST_EMAIL` — test owner email.
- `E2E_TEST_PASSWORD` — test owner password.
- `E2E_CUSTOMER_PHONE` — dedicated test WhatsApp customer number.
- `E2E_EXPECTED_SERVICE_NAME` — known configured test service.
- `E2E_META_TEST_NUMBER` — Meta test business number, if applicable.

The test owner account must belong to a dedicated non-production or isolated test tenant. Never use a real customer's account or phone number for automated acceptance. The Meta business number must be connected through the real Embedded Signup flow before the workflow is run.

Configure these GitHub Actions repository or environment variables/secrets:

- Variables: `E2E_BASE_URL`, `E2E_DASHBOARD_URL`, `E2E_EXPECTED_SERVICE_NAME`.
- Secrets: `E2E_TEST_TENANT_SLUG`, `E2E_TEST_EMAIL`, `E2E_TEST_PASSWORD`, `E2E_CUSTOMER_PHONE`.

After deploying the exact commit and configuring those values, run the **Live acceptance tests** workflow manually from GitHub Actions. Set `verify_customer_booking=true` only after the test customer has completed the real WhatsApp booking conversation. The dashboard job verifies the HttpOnly session and core screens. When `E2E_CUSTOMER_PHONE` and `E2E_EXPECTED_SERVICE_NAME` are configured, it also verifies that the confirmed booking appears in the appointments table and that the outbound confirmation is visible in the WhatsApp thread. The backend job checks API readiness, worker heartbeat, tenant data, WhatsApp connection and (when selected) the confirmed appointment and outbound confirmation message. The workflow does not send a customer WhatsApp message on its own.

## 6. Customer journey acceptance

Run the following in order:

1. Owner signs into the deployed dashboard using the supplied test account.
2. Confirm tenant identity, service catalogue, opening hours and active WhatsApp connection.
3. Send an actual inbound WhatsApp message from the designated test customer phone to the connected test business number.
4. Confirm the signed webhook is accepted and the inbound message appears in the correct tenant inbox.
5. Confirm the assistant lists active salon services.
6. Select a service and provide the customer name.
7. Request a date with configured availability.
8. Select one of the offered slots.
9. Confirm the displayed service, date and time with YES.
10. Confirm exactly one appointment is created and visible in the dashboard.
11. Confirm the outbound confirmation is visible in the conversation thread and Meta delivery status reconciles to delivered/read.
12. Replay the same webhook event and verify no duplicate appointment or outbound response.
13. Test an occupied slot and verify it is rejected/re-offered without creating a duplicate.
14. Test HUMAN escalation and owner resolution.
15. Test STOP/UNSUBSCRIBE and verify all subsequent outbound sends are blocked.
16. Test the 24-hour service-window boundary and verify free-form text sends fail closed outside the window.
17. Verify tenant B cannot read tenant A appointments, conversations or message bodies.
18. Inspect sanitized logs and dead-letter queue state for the test window.

A simulated webhook sent directly to the API is useful for integration tests but is not proof of real Meta delivery. At least one acceptance run must use an actual Meta test customer message and confirm a provider delivery status.

## 7. Operational monitoring

Configure external alerts for:

- `/health/ready` failures.
- `/health/worker` stale or unavailable status.
- Persistent webhook signature failures or webhook processing errors.
- Oldest queued job age and dead-letter count.
- Outbound messages in `unknown` or `failed` state.
- Meta access-token expiry approaching.
- Database CPU, storage, connections and backup failures.
- API 5xx rate, p95 latency and authentication 429 rate.

The application persists worker heartbeats and job states. `GET /internal/ops/summary` returns aggregate queue counts, oldest queued-job age, worker heartbeat states, outbound delivery states, webhook event states and tokens nearing expiry. Protect this endpoint with the `X-Platform-Admin-Key` header. It returns no customer message bodies, phone numbers or access tokens. Configure external alerts against these aggregate values; alert delivery still needs to be configured in the hosting/monitoring environment.

## 8. Backup and recovery acceptance

Before production approval:

1. Confirm automated PostgreSQL backups are enabled.
2. Record the selected backup retention and point-in-time recovery window.
3. Restore a backup into an isolated database.
4. Run `alembic current` and `alembic check` against the restored database.
5. Verify tenants, services, business hours, appointments, audit events, webhook events and job records.
6. Verify the separately backed-up Fernet key decrypts a controlled test fixture. Do not print decrypted secrets.
7. Document the restore duration and recovery point achieved.

No backup or restore claim should be made until this exercise has been completed and recorded.

## 9. Go-live gate

Do not enable external customer onboarding until all of the following evidence exists:

- CI green for the exact production commit.
- Production migrations applied successfully.
- API, worker and dashboard deployed and health checks green.
- Meta Embedded Signup v4 verified against the real test app/WABA.
- Actual customer-to-salon WhatsApp booking journey passes.
- Duplicate delivery and idempotency checks pass.
- Tenant isolation and auth throttling checks pass.
- STOP and 24-hour policy checks pass.
- Token expiry and recovery procedure documented.
- Backup restore drill completed.
- Monitoring and incident ownership assigned.
- Remaining known limitations accepted explicitly by the business owner.

Production deployment is an operator-controlled action. The repository's CI does not deploy the application automatically.
