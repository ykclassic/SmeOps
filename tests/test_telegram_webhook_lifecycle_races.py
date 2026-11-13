import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from starlette.requests import Request

from app.api.telegram import (
    receive_platform_telegram_webhook,
    receive_telegram_webhook,
)
from app.core.telegram import hash_telegram_webhook_secret
from app.db.session import AsyncSessionFactory
from app.models import (
    BackgroundJob,
    TelegramChatContext,
    TelegramConnection,
    TelegramPlatformBot,
    TelegramWebhookEvent,
    Tenant,
)


async def _wait_for_postgres_blocker(waiting_pid: int, blocking_pid: int) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while loop.time() < deadline:
        async with AsyncSessionFactory() as observer:
            blockers = await observer.scalar(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": waiting_pid}
            )
        if blocking_pid in (blockers or []):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"PostgreSQL did not report backend {waiting_pid} blocked by {blocking_pid}"
    )


def _telegram_request(path: str, payload: dict, secret: str) -> Request:
    body = json.dumps(payload).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"x-telegram-bot-api-secret-token", secret.encode()),
                (b"content-length", str(len(body)).encode()),
            ],
            "server": ("testserver", 80),
            "client": ("testclient", 123),
        },
        receive,
    )


@pytest.mark.parametrize("route_kind", ["platform", "legacy"])
@pytest.mark.asyncio
async def test_actual_telegram_webhook_routes_wait_for_deactivation_and_do_not_enqueue(
    route_kind: str,
):
    tenant_id = uuid4()
    platform_bot_id = uuid4()
    chat_id = str(810000000 + (uuid4().int % 100000000))
    secret = "adm012-webhook-secret"
    platform_bot_identifier = "9016622451"
    legacy_bot_identifier = "9016622452"

    async with AsyncSessionFactory() as setup:
        async with setup.begin():
            setup.add(
                Tenant(
                    id=tenant_id,
                    name="ADM-012 webhook race",
                    slug=f"adm012-webhook-{uuid4().hex}",
                    status="active",
                    business_timezone="Africa/Lagos",
                )
            )
            await setup.flush()
            setup.add(
                TelegramPlatformBot(
                    id=platform_bot_id,
                    singleton_key="platform",
                    bot_id=platform_bot_identifier,
                    bot_username="smeops_test_bot",
                    bot_token_encrypted="encrypted-test-token",
                    webhook_secret_hash=hash_telegram_webhook_secret(secret),
                    status="active",
                )
            )
            # Persist the referenced platform bot before inserting its chat context.
            await setup.flush()
            setup.add(
                TelegramChatContext(
                    platform_bot_id=platform_bot_id,
                    tenant_id=tenant_id,
                    telegram_user_id=chat_id,
                    telegram_chat_id=chat_id,
                )
            )
            setup.add(
                TelegramConnection(
                    tenant_id=tenant_id,
                    bot_id=legacy_bot_identifier,
                    bot_username="legacy_test_bot",
                    bot_token_encrypted="encrypted-legacy-token",
                    webhook_secret_hash=hash_telegram_webhook_secret(secret),
                    status="active",
                )
            )

    payload = {
        "update_id": 810001,
        "message": {
            "message_id": 1,
            "date": 1_800_000_000,
            "chat": {"id": int(chat_id), "type": "private"},
            "from": {"id": int(chat_id), "is_bot": False, "first_name": "Test"},
            "text": "hello",
        },
    }

    try:
        async with AsyncSessionFactory() as lifecycle_session:
            async with lifecycle_session.begin():
                lifecycle_pid = await lifecycle_session.scalar(
                    text("SELECT pg_backend_pid()")
                )
                # Hold the tenant mutex while deactivation wins. The webhook
                # request must block here and re-read the committed inactive state.
                await lifecycle_session.execute(
                    text("UPDATE tenants SET status = 'deactivated' WHERE id = :id"),
                    {"id": tenant_id},
                )

                request_started = asyncio.Event()
                request_pid: int | None = None

                async def invoke_route():
                    nonlocal request_pid
                    async with AsyncSessionFactory() as request_session:
                        request_pid = await request_session.scalar(
                            text("SELECT pg_backend_pid()")
                        )
                        request_started.set()
                        request = _telegram_request(
                            (
                                "/webhooks/telegram/platform"
                                if route_kind == "platform"
                                else f"/webhooks/telegram/{legacy_bot_identifier}"
                            ),
                            payload,
                            secret,
                        )
                        if route_kind == "platform":
                            return await receive_platform_telegram_webhook(
                                request,
                                request_session,
                                secret,
                            )
                        return await receive_telegram_webhook(
                            legacy_bot_identifier,
                            request,
                            request_session,
                            secret,
                        )

                task = asyncio.create_task(invoke_route())
                await asyncio.wait_for(request_started.wait(), timeout=3)
                assert request_pid is not None
                await _wait_for_postgres_blocker(request_pid, lifecycle_pid)
                assert not task.done()

            response = await asyncio.wait_for(task, timeout=5)
            assert response == {"status": "ignored", "queued_events": 0}

        async with AsyncSessionFactory() as verify:
            assert await verify.scalar(
                select(func.count()).select_from(TelegramWebhookEvent).where(
                    TelegramWebhookEvent.tenant_id == tenant_id
                )
            ) == 0
            assert await verify.scalar(
                select(func.count()).select_from(BackgroundJob).where(
                    BackgroundJob.tenant_id == tenant_id
                )
            ) == 0
            tenant = await verify.get(Tenant, tenant_id)
            assert tenant is not None and tenant.status == "deactivated"
    finally:
        async with AsyncSessionFactory() as cleanup:
            async with cleanup.begin():
                await cleanup.execute(
                    text("DELETE FROM background_jobs WHERE tenant_id = :id"),
                    {"id": tenant_id},
                )
                await cleanup.execute(
                    text("DELETE FROM telegram_webhook_events WHERE tenant_id = :id"),
                    {"id": tenant_id},
                )
                await cleanup.execute(
                    text("DELETE FROM telegram_chat_contexts WHERE tenant_id = :id"),
                    {"id": tenant_id},
                )
                await cleanup.execute(
                    text("DELETE FROM telegram_connections WHERE tenant_id = :id"),
                    {"id": tenant_id},
                )
                await cleanup.execute(
                    text("DELETE FROM telegram_platform_bots WHERE id = :id"),
                    {"id": platform_bot_id},
                )
                await cleanup.execute(
                    text("DELETE FROM tenants WHERE id = :id"),
                    {"id": tenant_id},
                )
