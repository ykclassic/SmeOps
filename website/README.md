# SmeOps public website

Static public-facing product website intended to support product discovery and Meta app review.

## Pages

- `index.html` — product overview, workflow and feature description.
- `privacy.html` — privacy practices and data categories.
- `terms.html` — terms of use for pilot/testing.
- `contact.html` — support and privacy-request route.
- `styles.css` — responsive styles.

## Deploy

Deploy this directory as a static site on Vercel (or another static host):

1. Create a separate Vercel project linked to this repository.
2. Set **Root Directory** to `website`.
3. Use the static/no-build output configuration (no build command; output directory is `.`).
4. Add the approved production domain to the Meta app's App Domains and Valid OAuth Redirect URIs where applicable.
5. Configure the same domain in the Meta Embedded Signup / Facebook Login settings and the relevant WhatsApp webhook/app settings.
6. Verify all pages over HTTPS and ensure the privacy policy URL is publicly accessible without login.

## Before submitting for Meta review

- Replace GitHub Issues as the contact route with a monitored, private support/privacy contact channel before accepting real personal-data requests.
- Review the Privacy Policy and Terms with the service operator and legal adviser; confirm controller/processor roles, retention periods, legal basis, data-subject request workflow and cross-border safeguards.
- Confirm the public website's product statements match the exact app-review screencast and requested permissions.
- Keep the product in test mode until live Meta app permissions, webhook delivery and customer journeys have been independently tested.

This website describes the implemented SmeOps capabilities conservatively. It does not claim that the service has completed production acceptance or Meta approval.
