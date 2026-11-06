# Free-tier integrated API worker

SmeOps staging runs the FastAPI application and the durable PostgreSQL job consumer in one Render Free web-service process. This avoids a separately billed Render background worker.

## Runtime model

- FastAPI starts the worker task from its lifespan only when `APP_ENV=staging` or `APP_ENV=production`.
- The worker uses the existing PostgreSQL-backed job queue and existing handlers for inbound WhatsApp events, signup completion, conversation processing, and outbound messages.
- A PostgreSQL session-level advisory lock allows only one embedded worker to consume jobs across overlapping processes or instances.
- The lock is held on a dedicated database connection and released on orderly shutdown. PostgreSQL releases it automatically if that connection or process disappears.
- Uvicorn must run with exactly one process per API instance. Do not use `--workers 2` or add a separate worker service with this configuration.
- `/health/worker` reports healthy only when a worker heartbeat is no more than 90 seconds old.

The worker uses a separate database session for each job operation. The advisory-lock connection is kept separate from application sessions so a handler cannot accidentally release the singleton lock by committing its own transaction.

## Render Free limitations

This is a staging/development cost-control arrangement, not production-grade continuous processing.

Render Free web services spin down after a period without inbound traffic. While the API is asleep, the embedded worker is asleep too and cannot process queued jobs. The first inbound webhook may incur a cold start. Do not assume Meta webhook delivery will wait indefinitely for the service to wake; validate webhook retry behaviour and end-to-end delivery before relying on this arrangement.

A free web service also has limited compute and database connection capacity. The API and worker share the same process and database connection pool. Avoid increasing Uvicorn processes or adding polling loops without measuring load.

## Deploying schema changes

The free Render blueprint does not use a paid pre-deploy command. Apply migrations using the manually dispatched **Staging database migrations** GitHub Actions workflow documented in [free-staging-database-migrations.md](free-staging-database-migrations.md). Confirm the migration workflow succeeds before deploying code that depends on a new schema.

## Verification after deployment

1. Confirm Render is running the intended commit and the service is awake.
2. Request `/health/live` and `/health/ready`.
3. Request `/health/worker`; it should return HTTP 200 with `worker=running` and a recent `last_seen_at`.
4. Queue a harmless test job using the application's supported test path, then verify it is completed once and the worker heartbeat continues to advance.
5. Restart/redeploy the service and verify a fresh heartbeat appears after startup.
6. Confirm `/health/worker` returns HTTP 503 when no fresh running heartbeat exists.

Do not manually insert arbitrary production jobs or use live WhatsApp sends as the first worker test.

## Recovery and delivery semantics

The job queue remains durable in PostgreSQL. A process restart does not erase queued rows. Jobs claimed by a process that dies must become eligible again through the existing lease-expiry/retry rules. Job handlers must remain idempotent because a process can fail after an external side effect succeeds but before the queue records completion.

The worker attempts a graceful stop for up to 20 seconds during FastAPI shutdown. If a handler does not finish in that window, the task is cancelled; its lease/recovery path must make the job retryable. This cannot guarantee exactly-once delivery to external providers.

## Production path

Before production use, replace the Free-tier arrangement with an always-on worker deployment or another supported always-on execution platform, add operational alerting and queue-lag metrics, and validate webhook acknowledgement/retry behaviour under cold starts, process restarts, provider timeouts, and database outages.
