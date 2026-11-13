import asyncio
import hashlib
import hmac
import json
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from starlette.requests import Request

from app.api.whatsapp import receive_whatsapp_webhook
from app.core.config import get_settings
from app.core.whatsapp import encrypt_whatsapp_secret
from app.db.session import AsyncSessionFactory
from app.models import BackgroundJob, Tenant, WhatsAppConnection, WhatsAppWebhookEvent


APP_SECRET = "adm-013-test-whatsapp-secret"
ENCRYPTION_KEY = "c21lb3BzLWNpLXdoYXRzYXBwLWZlcm5ldC1rZXktMzI="
PHONE_NUMBER_ID = "12345678901"


@pytest.fixture(autouse=True)
def whatsapp_webhook_settings(monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("WHATSAPP_TOKEN_ENCRYPTION_KEY", ENCRYPTION_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _signed_request(payload: dict) -> Request:
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(
        APP_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/webhooks/whatsapp",
            "raw_path": b"/webhooks/whatsapp",
            "query_string": b"",
            "headers": [(b"x-hub-signature-256", signature.encode())],
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 12345),
        },
        receive,
    )


def _payload(message_id: str) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "messages": [
                                {
                                    "from": "2348012345678",
                                    "id": message_id,
                                    "timestamp": "1793433600",
                                    "type": "text",
                                    "text": {"body": "BOOK"},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }


async def _create_tenant(status: str = "active") -> Tenant:
    tenant = Tenant(
        id=uuid4(),
        name="ADM-013 WhatsApp lifecycle test",
        slug=f"adm013-{uuid4().hex[:12]}",
        business_timezone="Africa/Lagos",
        status=status,
    )
    connection = WhatsAppConnection(
        tenant_id=tenant.id,
        phone_number_id=PHONE_NUMBER_ID,
        business_account_id=f"{uuid4().int % 10**11:011d}",
        access_token_encrypted=encrypt_whatsapp_secret("adm-013-test-access-token"),
        status="active",
    )
    async with AsyncSessionFactory() as session:
        session.add_all([tenant, connection])
        await session.commit()
    return tenant


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_status", ["deactivated", "suspended", "pending"])
async def test_inactive_tenant_webhook_is_acknowledged_without_event_or_job(
    tenant_status: str,
):
    tenant = await _create_tenant(tenant_status)
    async with AsyncSessionFactory() as session:
        response = await receive_whatsapp_webhook(
            _signed_request(_payload(f"wamid.{uuid4().hex}")),
            session,
        )
        await session.commit()

    assert response == {"status": "accepted", "queued_events": 0}
    async with AsyncSessionFactory() as session:
        events = await session.execute(
            select(WhatsAppWebhookEvent).where(
                WhatsAppWebhookEvent.tenant_id == tenant.id
            )
        )
        jobs = await session.execute(
            select(BackgroundJob).where(
                BackgroundJob.tenant_id == tenant.id,
                BackgroundJob.job_type == "whatsapp.accept_inbound",
            )
        )
        assert events.scalars().all() == []
        assert jobs.scalars().all() == []


@pytest.mark.asyncio
async def test_webhook_waits_for_tenant_lifecycle_lock_and_rechecks_committed_state():
    tenant = await _create_tenant("active")
    message_id = f"wamid.{uuid4().hex}"

    async with AsyncSessionFactory() as lifecycle_session:
        await lifecycle_session.execute(
            select(Tenant).where(Tenant.id == tenant.id).with_for_update()
        )

        async with AsyncSessionFactory() as webhook_session:
            webhook_pid = await webhook_session.scalar(
                text("SELECT pg_backend_pid()")
            )
            webhook_task = asyncio.create_task(
                receive_whatsapp_webhook(
                    _signed_request(_payload(message_id)),
                    webhook_session,
                )
            )

            # Prove that the actual webhook handler is blocked by the tenant
            # row lock, rather than relying on a sleep-based race assertion.
            blocked = False
            for _ in range(100):
                blockers = await lifecycle_session.scalar(
                    text("SELECT pg_blocking_pids(:pid)"),
                    {"pid": webhook_pid},
                )
                if blockers:
                    blocked = True
                    break
                await asyncio.sleep(0.02)
            assert blocked, "webhook did not wait on the tenant lifecycle mutex"

            locked_tenant = await lifecycle_session.get(Tenant, tenant.id)
            assert locked_tenant is not None
            locked_tenant.status = "deactivated"
            await lifecycle_session.commit()

            response = await asyncio.wait_for(webhook_task, timeout=5)
            await webhook_session.commit()

    assert response == {"status": "accepted", "queued_events": 0}
    async with AsyncSessionFactory() as session:
        events = await session.execute(
            select(WhatsAppWebhookEvent).where(
                WhatsAppWebhookEvent.tenant_id == tenant.id
            )
        )
        jobs = await session.execute(
            select(BackgroundJob).where(
                BackgroundJob.tenant_id == tenant.id,
                BackgroundJob.job_type == "whatsapp.accept_inbound",
            )
        )
        assert events.scalars().all() == []
        assert jobs.scalars().all() == []
