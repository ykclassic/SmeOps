import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.telegram import encrypt_telegram_secret
from app.db.session import AsyncSessionFactory
from app.jobs.queue import ClaimedJob
from app.main import app
from app.models import (
    Appointment,
    BackgroundJob,
    TelegramConversation,
    TelegramConversationMessage,
    TelegramOutboundMessage,
    TelegramOwnerLink,
    TelegramOwnerNotification,
    TelegramPlatformBot,
    Tenant,
)
from app.services.tenant_lifecycle import transition_tenant_lifecycle


def _register(client: TestClient, slug: str, email: str) -> str:
    response = client.post(
        "/auth/register",
        json={
            "business_name": "Appointment Notification Integration",
            "tenant_slug": slug,
            "email": email,
            "password": "Correct-Horse-Battery-42",
            "business_timezone": "Africa/Lagos",
        },
    )
    assert response.status_code == 201
    return response.json()["access_token"]


@pytest.mark.asyncio
async def test_committed_appointment_owner_notification_delivers_after_deactivation(
    monkeypatch,
):
    monkeypatch.setenv(
        "WHATSAPP_TOKEN_ENCRYPTION_KEY",
        "c21lb3BzLWNpLXdoYXRzYXBwLWZlcm5ldC1rZXktMzI=",
    )
    get_settings.cache_clear()
    tenant_id = None
    platform_bot_id = None
    platform_bot_created = False
    platform_bot_snapshot = None

    try:
        slug = f"appt-notify-{uuid4().hex[:12]}"
        with TestClient(app) as client:
            token = _register(client, slug, f"{slug}@example.com")
            headers = {"Authorization": f"Bearer {token}"}
            identity = client.get("/auth/me", headers=headers)
            assert identity.status_code == 200
            tenant_id = UUID(identity.json()["tenant_id"])
            owner_id = UUID(identity.json()["id"])

            service_response = client.post(
                "/services",
                headers=headers,
                json={
                    "name": "Notification Integration Service",
                    "duration_minutes": 60,
                    "price_minor": 2500000,
                },
            )
            assert service_response.status_code == 201

            hours_response = client.put(
                "/business-hours",
                headers=headers,
                json={
                    "days": [
                        {
                            "weekday": weekday,
                            "is_closed": False,
                            "opens_at": "08:00:00",
                            "closes_at": "18:00:00",
                        }
                        for weekday in range(7)
                    ]
                },
            )
            assert hours_response.status_code == 200

            async with AsyncSessionFactory() as session:
                platform_bot = await session.scalar(
                    select(TelegramPlatformBot).where(
                        TelegramPlatformBot.singleton_key == "platform"
                    )
                )
                if platform_bot is None:
                    platform_bot = TelegramPlatformBot(
                        singleton_key="platform",
                        bot_id=f"test-{uuid4().hex}",
                        bot_username=f"test_{uuid4().hex[:12]}",
                        bot_token_encrypted=encrypt_telegram_secret(
                            "integration-test-token"
                        ),
                        webhook_secret_hash=hashlib.sha256(
                            uuid4().hex.encode()
                        ).hexdigest(),
                        status="active",
                    )
                    session.add(platform_bot)
                    platform_bot_created = True
                else:
                    platform_bot_snapshot = {
                        "bot_id": platform_bot.bot_id,
                        "bot_username": platform_bot.bot_username,
                        "bot_token_encrypted": platform_bot.bot_token_encrypted,
                        "webhook_secret_hash": platform_bot.webhook_secret_hash,
                        "status": platform_bot.status,
                    }
                    platform_bot.status = "active"
                    platform_bot.bot_token_encrypted = encrypt_telegram_secret(
                        "integration-test-token"
                    )
                await session.flush()
                platform_bot_id = platform_bot.id

                session.add(
                    TelegramOwnerLink(
                        tenant_id=tenant_id,
                        owner_user_id=owner_id,
                        invitation_hash=hashlib.sha256(
                            uuid4().hex.encode()
                        ).hexdigest(),
                        expires_at=datetime.now(UTC) + timedelta(days=1),
                        chat_id=f"owner-chat-{uuid4().hex[:8]}",
                        linked_at=datetime.now(UTC),
                    )
                )
                await session.commit()

            local_start = (
                datetime.now(ZoneInfo("Africa/Lagos")) + timedelta(days=3)
            ).replace(hour=10, minute=0, second=0, microsecond=0)
            booking_response = client.post(
                "/appointments",
                headers=headers,
                json={
                    "idempotency_key": str(uuid4()),
                    "service_id": service_response.json()["id"],
                    "customer_name": "Integration Test Customer",
                    "customer_phone": "+2348012345678",
                    "starts_at": local_start.isoformat(),
                },
            )
            assert booking_response.status_code == 201
            appointment_id = UUID(booking_response.json()["id"])

        async with AsyncSessionFactory() as session:
            appointment = await session.get(Appointment, appointment_id)
            notification = await session.scalar(
                select(TelegramOwnerNotification).where(
                    TelegramOwnerNotification.tenant_id == tenant_id,
                    TelegramOwnerNotification.appointment_id == appointment_id,
                    TelegramOwnerNotification.event_type == "appointment.created",
                )
            )
            assert appointment is not None and appointment.status == "confirmed"
            assert notification is not None and notification.status == "queued"
            notification_id = notification.id
            queued_jobs = (
                await session.scalars(
                    select(BackgroundJob).where(
                        BackgroundJob.tenant_id == tenant_id,
                        BackgroundJob.job_type == "telegram.owner_appointment_alert",
                    )
                )
            ).all()
            queued_job = next(
                (
                    candidate
                    for candidate in queued_jobs
                    if candidate.payload.get("notification_id") == str(notification_id)
                ),
                None,
            )
            assert queued_job is not None and queued_job.status == "queued"
            job_id = queued_job.id

        async with AsyncSessionFactory() as session:
            async with session.begin():
                result = await transition_tenant_lifecycle(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=owner_id,
                    target_status="deactivated",
                    reason="Integration test: preserve committed appointment notification",
                )
                assert result.changed is True

        sent = []

        async def fake_send_text_message(*, bot_token, chat_id, body):
            sent.append((bot_token, chat_id, body))
            return type(
                "TelegramSendResult",
                (),
                {"provider_message_id": "integration-provider-message"},
            )()

        monkeypatch.setattr(
            "app.jobs.telegram_owner_notifications.send_text_message",
            fake_send_text_message,
        )
        from app.jobs.telegram_owner_notifications import send_owner_appointment_alert_job

        await send_owner_appointment_alert_job(
            ClaimedJob(
                id=job_id,
                tenant_id=tenant_id,
                job_type="telegram.owner_appointment_alert",
                payload={"notification_id": str(notification_id)},
                attempts=1,
                max_attempts=3,
            )
        )

        assert len(sent) == 1
        bot_token, chat_id, body = sent[0]
        assert bot_token == "integration-test-token"
        assert chat_id.startswith("owner-chat-")
        assert "NEW APPOINTMENT CONFIRMED" in body
        assert str(appointment_id).replace("-", "")[:8].upper() in body

        async with AsyncSessionFactory() as session:
            tenant = await session.get(Tenant, tenant_id)
            appointment = await session.get(Appointment, appointment_id)
            notification = await session.get(
                TelegramOwnerNotification, notification_id
            )
            assert tenant is not None and tenant.status == "deactivated"
            assert appointment is not None and appointment.status == "confirmed"
            assert notification is not None and notification.status == "sent"
            assert notification.provider_message_id == (
                f"{chat_id}:integration-provider-message"
            )
            assert notification.sent_at is not None
    finally:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                if tenant_id is not None:
                    await session.execute(
                        delete(Tenant).where(Tenant.id == tenant_id)
                    )
                if platform_bot_id is not None:
                    platform_bot = await session.get(
                        TelegramPlatformBot, platform_bot_id
                    )
                    if platform_bot_created:
                        if platform_bot is not None:
                            await session.delete(platform_bot)
                    elif platform_bot is not None and platform_bot_snapshot is not None:
                        for attribute, value in platform_bot_snapshot.items():
                            setattr(platform_bot, attribute, value)
        get_settings.cache_clear()



@pytest.mark.asyncio
async def test_customer_cancellation_reply_delivers_after_deactivation(monkeypatch):
    monkeypatch.setenv(
        "WHATSAPP_TOKEN_ENCRYPTION_KEY",
        "c21lb3BzLWNpLXdoYXRzYXBwLWZlcm5ldC1rZXktMzI=",
    )
    get_settings.cache_clear()
    tenant_id = None
    platform_bot_id = None
    platform_bot_created = False
    platform_bot_snapshot = None

    async def no_op_provisioning(**_kwargs):
        return None

    try:
        slug = f"customer-notify-{uuid4().hex[:12]}"
        with TestClient(app) as client:
            token = _register(client, slug, f"{slug}@example.com")
            headers = {"Authorization": f"Bearer {token}"}
            identity = client.get("/auth/me", headers=headers)
            assert identity.status_code == 200
            tenant_id = UUID(identity.json()["tenant_id"])
            owner_id = UUID(identity.json()["id"])

            service_response = client.post(
                "/services",
                headers=headers,
                json={
                    "name": "Customer Cancellation Integration Service",
                    "duration_minutes": 60,
                    "price_minor": 2500000,
                },
            )
            assert service_response.status_code == 201
            hours_response = client.put(
                "/business-hours",
                headers=headers,
                json={
                    "days": [
                        {
                            "weekday": weekday,
                            "is_closed": False,
                            "opens_at": "08:00:00",
                            "closes_at": "18:00:00",
                        }
                        for weekday in range(7)
                    ]
                },
            )
            assert hours_response.status_code == 200

            async with AsyncSessionFactory() as session:
                platform_bot = await session.scalar(
                    select(TelegramPlatformBot).where(
                        TelegramPlatformBot.singleton_key == "platform"
                    )
                )
                if platform_bot is None:
                    platform_bot = TelegramPlatformBot(
                        singleton_key="platform",
                        bot_id=f"test-{uuid4().hex}",
                        bot_username=f"test_{uuid4().hex[:12]}",
                        bot_token_encrypted=encrypt_telegram_secret(
                            "customer-integration-token"
                        ),
                        webhook_secret_hash=hashlib.sha256(
                            uuid4().hex.encode()
                        ).hexdigest(),
                        status="active",
                    )
                    session.add(platform_bot)
                    platform_bot_created = True
                else:
                    platform_bot_snapshot = {
                        "bot_id": platform_bot.bot_id,
                        "bot_username": platform_bot.bot_username,
                        "bot_token_encrypted": platform_bot.bot_token_encrypted,
                        "webhook_secret_hash": platform_bot.webhook_secret_hash,
                        "status": platform_bot.status,
                    }
                    platform_bot.status = "active"
                    platform_bot.bot_token_encrypted = encrypt_telegram_secret(
                        "customer-integration-token"
                    )
                await session.flush()
                platform_bot_id = platform_bot.id
                await session.commit()

            local_start = (
                datetime.now(ZoneInfo("Africa/Lagos")) + timedelta(days=3)
            ).replace(hour=10, minute=0, second=0, microsecond=0)
            booking_response = client.post(
                "/appointments",
                headers=headers,
                json={
                    "idempotency_key": str(uuid4()),
                    "service_id": service_response.json()["id"],
                    "customer_name": "Customer Notification Test",
                    "customer_phone": "+2348012345678",
                    "starts_at": local_start.isoformat(),
                },
            )
            assert booking_response.status_code == 201
            appointment_id = UUID(booking_response.json()["id"])

        conversation_id = uuid4()
        inbound_message_id = uuid4()
        telegram_user_id = f"customer-user-{uuid4().hex[:12]}"
        telegram_chat_id = f"customer-chat-{uuid4().hex[:12]}"
        async with AsyncSessionFactory() as session:
            appointment = await session.get(Appointment, appointment_id)
            assert appointment is not None
            session.add(
                TelegramConversation(
                    id=conversation_id,
                    tenant_id=tenant_id,
                    telegram_user_id=telegram_user_id,
                    telegram_chat_id=telegram_chat_id,
                    status="active",
                    context={
                        "appointment_management": {
                            "action": "cancel",
                            "step": "confirm_cancel",
                            "customer_id": str(appointment.customer_id),
                            "appointment_id": str(appointment_id),
                            "expires_at": (
                                datetime.now(UTC) + timedelta(minutes=10)
                            ).isoformat(),
                        }
                    },
                    last_inbound_at=datetime.now(UTC),
                )
            )
            session.add(
                TelegramConversationMessage(
                    id=inbound_message_id,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    provider_message_id=f"{telegram_chat_id}:customer-cancel",
                    direction="inbound",
                    body_encrypted=encrypt_telegram_secret("YES"),
                    delivery_status="received",
                )
            )
            await session.commit()

        monkeypatch.setattr(
            "app.services.conversation_engine.ensure_private_chat_commands",
            no_op_provisioning,
        )
        from app.services.conversation_engine import process_telegram_conversation_job

        await process_telegram_conversation_job(
            ClaimedJob(
                id=uuid4(),
                tenant_id=tenant_id,
                job_type="telegram.process_conversation",
                payload={
                    "conversation_id": str(conversation_id),
                    "message_id": str(inbound_message_id),
                },
                attempts=1,
                max_attempts=3,
            )
        )

        async with AsyncSessionFactory() as session:
            appointment = await session.get(Appointment, appointment_id)
            outbound = await session.scalar(
                select(TelegramOutboundMessage).where(
                    TelegramOutboundMessage.tenant_id == tenant_id,
                    TelegramOutboundMessage.conversation_id == conversation_id,
                    TelegramOutboundMessage.idempotency_key
                    == f"conversation-reply:{inbound_message_id}",
                )
            )
            assert appointment is not None and appointment.status == "cancelled"
            assert outbound is not None
            assert outbound.status == "queued"
            assert outbound.deliver_after_deactivation is True
            outbound_id = outbound.id

        async with AsyncSessionFactory() as session:
            async with session.begin():
                result = await transition_tenant_lifecycle(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=owner_id,
                    target_status="deactivated",
                    reason="Integration test: preserve committed customer cancellation reply",
                )
                assert result.changed is True

        sent = []

        async def fake_send_text_message(*, bot_token, chat_id, body, reply_markup=None):
            sent.append((bot_token, chat_id, body, reply_markup))
            return type(
                "TelegramSendResult",
                (),
                {"provider_message_id": "customer-integration-provider-message"},
            )()

        monkeypatch.setattr(
            "app.jobs.telegram_outbound.send_text_message",
            fake_send_text_message,
        )
        from app.jobs.telegram_outbound import send_outbound_telegram_job

        await send_outbound_telegram_job(
            ClaimedJob(
                id=uuid4(),
                tenant_id=tenant_id,
                job_type="telegram.send_text",
                payload={"message_id": str(outbound_id)},
                attempts=1,
                max_attempts=2,
            )
        )

        assert len(sent) == 1
        bot_token, chat_id, body, _reply_markup = sent[0]
        assert bot_token == "customer-integration-token"
        assert chat_id == telegram_chat_id
        assert "Your appointment has been cancelled" in body

        async with AsyncSessionFactory() as session:
            tenant = await session.get(Tenant, tenant_id)
            appointment = await session.get(Appointment, appointment_id)
            outbound = await session.get(TelegramOutboundMessage, outbound_id)
            assert tenant is not None and tenant.status == "deactivated"
            assert appointment is not None and appointment.status == "cancelled"
            assert outbound is not None and outbound.status == "sent"
            assert outbound.deliver_after_deactivation is True
            assert outbound.provider_message_id == (
                f"{telegram_chat_id}:customer-integration-provider-message"
            )
    finally:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                if tenant_id is not None:
                    await session.execute(
                        delete(Tenant).where(Tenant.id == tenant_id)
                    )
                if platform_bot_id is not None:
                    platform_bot = await session.get(
                        TelegramPlatformBot, platform_bot_id
                    )
                    if platform_bot_created:
                        if platform_bot is not None:
                            await session.delete(platform_bot)
                    elif platform_bot is not None and platform_bot_snapshot is not None:
                        for attribute, value in platform_bot_snapshot.items():
                            setattr(platform_bot, attribute, value)
        get_settings.cache_clear()
