# Free-tier staging database migrations

SmeOps staging uses the existing Render Free PostgreSQL instance. Render's paid pre-deploy command is not required: a manually dispatched GitHub Actions workflow applies Alembic migrations from an Ubuntu runner.

## One-time GitHub configuration

1. Open the SmeOps repository's **Settings → Environments**.
2. Create an environment named `staging`.
3. Open the Render dashboard for the staging PostgreSQL database.
4. Copy its **External Database URL**. Do not use the internal URL: GitHub-hosted runners are outside Render's private network.
5. In the GitHub `staging` environment, add an environment secret named `DATABASE_URL` containing that external connection URL.
6. Do not paste the URL into an issue, pull request, workflow input, source file, or chat. Do not use a production database URL for this environment.

The workflow never prints the connection URL. GitHub masks configured secrets in Actions logs, but avoiding output is the primary safeguard.

## Run a migration

1. Open **Actions → Staging database migrations → Run workflow**.
2. Select branch `main`.
3. Set `confirm_staging` to `yes`.
4. Type `smeops` into `database_name`.
5. Run the workflow and wait for it to finish.

The workflow:
- Runs only when dispatched against `main`.
- Uses the `staging` GitHub environment and its `DATABASE_URL` secret.
- Serializes migration runs so two runs cannot mutate the database concurrently.
- Confirms the supplied URL points to a non-local PostgreSQL database named `smeops`.
- Installs the application dependencies, runs `alembic upgrade head`, then runs `alembic current` and `alembic check`.
- Fails closed if confirmation or the database secret is missing.

The operation is repeatable: Alembic applies only unapplied revisions. Do not manually create application tables or edit `alembic_version`.

## After a successful run

Use the Render read-only database query facility or a separate read-only SQL client to verify:
- `public.alembic_version` exists and records the repository's current migration head.
- Expected application tables exist in `public`.
- The workflow's `alembic check` step passed.

Then verify the API's database-backed endpoints. The current `/health/ready` implementation only runs `SELECT 1`; a successful response alone is not evidence that migrations are applied. A schema-aware readiness check is a separate follow-up change and should be deployed only after the staging schema has been initialized.

## Free-tier constraints and operational safeguards

- The Render Free PostgreSQL database is temporary and currently has an expiry date of **2026-12-04**. Export a backup and plan a controlled migration before expiry; do not assume the database is permanent.
- Use the external connection URL only in the GitHub environment secret. Rotate it if it is exposed.
- The free database may have connection, storage, availability, and expiry limitations. Do not use it for production customer data.
- The API and worker must use the same staging database. Do not start a worker against an uninitialized schema.
- This workflow migrates the schema; it does not provision a worker, verify WhatsApp delivery, or establish production readiness.
