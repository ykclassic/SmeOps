# SmeOps owner dashboard

Next.js App Router dashboard for business owners and managers.

## Local development

1. Install Node.js 22 or later.
2. Copy `.env.example` to `.env.local`.
3. Set `SMEOPS_API_BASE_URL` to the FastAPI API origin (default local API: `http://localhost:8000`).
4. Install dependencies with `npm install`.
5. Start the dashboard with `npm run dev`.
6. Open `http://localhost:3000`.

The browser calls same-origin Next.js route handlers. The route handler proxies only an explicit allowlist of business API routes and stores the backend access token in an HttpOnly, SameSite=Strict cookie. The token is not returned to client JavaScript or written to localStorage.

## Deployment

Set `SMEOPS_API_BASE_URL` as a server-side environment variable in the frontend hosting environment. Do not prefix it with `NEXT_PUBLIC_`. The API must be reachable from the Next.js server over HTTPS in production.

Meta Embedded Signup configuration variables are documented here but are not active until the onboarding increment is completed:
- `NEXT_PUBLIC_META_APP_ID`
- `NEXT_PUBLIC_META_EMBEDDED_SIGNUP_CONFIG_ID`
- `NEXT_PUBLIC_META_GRAPH_API_VERSION`

## Included screens

- Authentication and business registration.
- Overview and setup checklist.
- Appointment list and cancellation.
- WhatsApp inbox and message history.
- Human-handoff resolution.
- Service catalogue management.
- Weekly business-hours management.
- WhatsApp connection status.

The dashboard currently targets the first salon pilot. It does not yet include staff/resource calendars, payments, reporting exports, or full WhatsApp Embedded Signup.
