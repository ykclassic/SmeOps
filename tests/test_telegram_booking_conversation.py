import asyncio
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.telegram import (
    decrypt_telegram_secret,
    encrypt_telegram_secret,
    hash_telegram_webhook_secret,
)
from app.db.session import AsyncSessionFactory
from app.jobs.queue import ClaimedJob
from app.jobs.telegram import accept_inbound_telegram_job
from app.main import app
from app.models import (
    Appointment,
    BackgroundJob,
    Customer,
    TelegramConversationMessage,
    TelegramCustomerIdentity,
    TelegramOutboundMessage,
    TelegramOwnerNotification,
    TelegramPlatformBot,
    Tenant,
)
from app.services.conversation_engine import process_telegram_conversation_job
from app.services.salon_booking import (
    BookingConflict,
    create_appointment_booking,
    get_available_slots,
)


@pytest.mark.parametrize("phone_input, expected_phone", [
    ("+2348012345678", "+2348012345678"),
    ("08012345678", "+2348012345678"),
    ("2348012345678", "+2348012345678"),
    ("__contact__", "+2348012345678"),
])
def test_telegram_booking_journey_and_cross_channel_conflict(
    monkeypatch,
    phone_input,
    expected_phone,
):
    encryption_key = "c21lb3BzLWNpLXdoYXRzYXBwLWZlcm5ldC1rZXktMzI="
    monkeypatch.setenv("WHATSAPP_TOKEN_ENCRYPTION_KEY", encryption_key)
    monkeypatch.setenv("APP_SECRET_KEY", "telegram-booking-test-secret-" + "x" * 20)
    monkeypatch.setenv("PUBLIC_API_BASE_URL", "https://api.example.com")
    get_settings.cache_clear()

    from app.api import telegram as telegram_api

    async def fake_ensure_platform_webhook(_platform_bot):
        return None

    monkeypatch.setattr(
        telegram_api,
        "_ensure_platform_webhook",
        fake_ensure_platform_webhook,
    )

    slug = f"telegram-booking-{uuid4().hex[:10]}"
    tenant_id: UUID | None = None
    chat_id = 998877665
    user_id = 998877665

    try:
        with TestClient(app) as client:
            registration = client.post(
                "/auth/register",
                json={
                    "business_name": "Telegram Booking Salon",
                    "tenant_slug": slug,
                    "email": f"{slug}@example.com",
                    "password": "Correct-Horse-Battery-42",
                    "business_timezone": "Africa/Lagos",
                },
            )
            assert registration.status_code == 201
            auth = {"Authorization": f"Bearer {registration.json()['access_token']}"}
            identity = client.get("/auth/me", headers=auth)
            tenant_id = UUID(identity.json()["tenant_id"])

            service = client.post(
                "/services",
                headers=auth,
                json={
                    "name": "Haircut",
                    "description": "Wash and haircut",
                    "duration_minutes": 60,
                    "price_minor": 2500000,
                },
            )
            assert service.status_code == 201
            service_id = UUID(service.json()["id"])

            hours = client.put(
                "/business-hours",
                headers=auth,
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
            assert hours.status_code == 200

            platform_bot_id = uuid4()
            async def seed_platform_bot():
                async with AsyncSessionFactory() as session:
                    existing = await session.execute(
                        select(TelegramPlatformBot).where(
                            TelegramPlatformBot.singleton_key == "platform"
                        )
                    )
                    if existing.scalar_one_or_none() is None:
                        session.add(
                            TelegramPlatformBot(
                                id=platform_bot_id,
                                singleton_key="platform",
                                bot_id="9016622451",
                                bot_username="smeops_bot",
                                bot_token_encrypted=encrypt_telegram_secret(
                                    "123456:telegram-test-token"
                                ),
                                webhook_secret_hash=hash_telegram_webhook_secret(
                                    telegram_api._telegram_webhook_secret("9016622451")
                                ),
                                status="active",
                            )
                        )
                        await session.commit()

            asyncio.run(seed_platform_bot())

            async def find_job(job_type: str, idempotency_key: str) -> ClaimedJob:
                async with AsyncSessionFactory() as session:
                    result = await session.execute(
                        select(BackgroundJob).where(
                            BackgroundJob.tenant_id == tenant_id,
                            BackgroundJob.job_type == job_type,
                            BackgroundJob.idempotency_key == idempotency_key,
                        )
                    )
                    job = result.scalar_one()
                    return ClaimedJob(
                        id=job.id,
                        tenant_id=job.tenant_id,
                        job_type=job.job_type,
                        payload=job.payload,
                        attempts=1,
                        max_attempts=job.max_attempts,
                    )

            link_response = client.post("/telegram/owner-link", headers=auth)
            assert link_response.status_code == 200
            assert link_response.json()["status"] == "pending"
            start_token = parse_qs(urlparse(link_response.json()["deep_link"]).query)["start"][0]
            owner_update_id = 799999
            owner_link_payload = {
                "update_id": owner_update_id,
                "message": {
                    "message_id": 900,
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": user_id, "is_bot": False, "first_name": "Ada"},
                    "text": f"/start {start_token}",
                },
            }
            owner_link_response = client.post(
                "/webhooks/telegram/platform",
                json=owner_link_payload,
                headers={
                    "X-Telegram-Bot-Api-Secret-Token":
                    telegram_api._telegram_webhook_secret("9016622451")
                },
            )
            assert owner_link_response.status_code == 200
            owner_link_job = asyncio.run(
                find_job("telegram.accept_inbound", f"tg:platform:9016622451:{owner_update_id}")
            )
            asyncio.run(accept_inbound_telegram_job(owner_link_job))
            owner_link_status = client.get("/telegram/owner-link", headers=auth)
            assert owner_link_status.status_code == 200
            assert owner_link_status.json()["linked"] is True

            booking_link_response = client.post("/telegram/booking-link", headers=auth)
            assert booking_link_response.status_code == 200
            booking_token = parse_qs(
                urlparse(booking_link_response.json()["booking_link"]).query
            )["start"][0]

            def send_customer_start() -> str:
                update_id = 800001
                payload = {
                    "update_id": update_id,
                    "message": {
                        "message_id": 1,
                        "date": int(datetime.now(UTC).timestamp()),
                        "chat": {"id": chat_id, "type": "private"},
                        "from": {
                            "id": user_id,
                            "is_bot": False,
                            "first_name": "Ada",
                        },
                        "text": f"/start {booking_token}",
                    },
                }
                response = client.post(
                    "/webhooks/telegram/platform",
                    json=payload,
                    headers={
                        "X-Telegram-Bot-Api-Secret-Token":
                        telegram_api._telegram_webhook_secret("9016622451")
                    },
                )
                assert response.status_code == 200, response.text
                assert response.json()["queued_events"] == 1
                return str(update_id)

            def send_customer_message(text: str, sequence: int) -> str:
                update_id = 800000 + sequence
                message_payload = {
                    "message_id": sequence,
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": chat_id, "type": "private"},
                    "from": {
                        "id": user_id,
                        "is_bot": False,
                        "first_name": "Ada",
                    },
                }
                if text == "__contact__":
                    message_payload["contact"] = {
                        "phone_number": "08012345678",
                        "user_id": user_id,
                    }
                else:
                    message_payload["text"] = text
                payload = {
                    "update_id": update_id,
                    "message": message_payload,
                }
                connection_secret = telegram_api._telegram_webhook_secret("9016622451")
                response = client.post(
                    "/webhooks/telegram/platform",
                    json=payload,
                    headers={"X-Telegram-Bot-Api-Secret-Token": connection_secret},
                )
                assert response.status_code == 200, response.text
                assert response.json()["queued_events"] == 1
                return str(update_id)

            async def process_inbound(update_id: str, sequence: int) -> None:
                inbound_job = await find_job(
                    "telegram.accept_inbound",
                    f"tg:platform:9016622451:{update_id}",
                )
                await accept_inbound_telegram_job(inbound_job)
                async with AsyncSessionFactory() as session:
                    message_result = await session.execute(
                        select(TelegramConversationMessage).where(
                            TelegramConversationMessage.tenant_id == tenant_id,
                            TelegramConversationMessage.provider_message_id
                            == f"{chat_id}:{sequence}",
                            TelegramConversationMessage.direction == "inbound",
                        )
                    )
                    inbound_message = message_result.scalar_one()
                conversation_job = await find_job(
                    "telegram.process_conversation",
                    f"tg:conversation:{inbound_message.id}",
                )
                await process_telegram_conversation_job(conversation_job)

            start_update_id = send_customer_start()
            asyncio.run(process_inbound(start_update_id, 1))

            steps = ("1", "Ada Lovelace", phone_input, "tomorrow", "1", "YES")
            for sequence, customer_text in enumerate(steps, start=2):
                update_id = send_customer_message(customer_text, sequence)
                asyncio.run(process_inbound(update_id, sequence))

            async def inspect_results():
                async with AsyncSessionFactory() as session:
                    appointments_result = await session.execute(
                        select(Appointment).where(Appointment.tenant_id == tenant_id)
                    )
                    appointments = list(appointments_result.scalars().all())
                    outbound_result = await session.execute(
                        select(TelegramOutboundMessage).where(
                            TelegramOutboundMessage.tenant_id == tenant_id
                        )
                    )
                    outbound = list(outbound_result.scalars().all())
                    history_result = await session.execute(
                        select(TelegramConversationMessage).where(
                            TelegramConversationMessage.tenant_id == tenant_id,
                            TelegramConversationMessage.direction == "outbound",
                        )
                    )
                    history = list(history_result.scalars().all())
                    customer_result = await session.execute(
                        select(Customer).where(Customer.tenant_id == tenant_id)
                    )
                    customers = list(customer_result.scalars().all())
                    notifications_result = await session.execute(
                        select(TelegramOwnerNotification).where(
                            TelegramOwnerNotification.tenant_id == tenant_id
                        )
                    )
                    notifications = list(notifications_result.scalars().all())
                    alert_jobs_result = await session.execute(
                        select(BackgroundJob).where(
                            BackgroundJob.tenant_id == tenant_id,
                            BackgroundJob.job_type == "telegram.owner_appointment_alert",
                        )
                    )
                    alert_jobs = list(alert_jobs_result.scalars().all())
                    return (
                        appointments,
                        outbound,
                        history,
                        customers,
                        notifications,
                        alert_jobs,
                    )

            (
                appointments,
                outbound,
                history,
                customers,
                notifications,
                alert_jobs,
            ) = asyncio.run(inspect_results())
            assert len(appointments) == 1
            appointment = appointments[0]
            assert appointment.status == "confirmed"
            assert len(outbound) == 7
            assert len(history) == 7
            assert len(customers) == 1
            assert customers[0].phone_e164 == expected_phone
            assert len(notifications) == 1
            assert notifications[0].status == "queued"
            assert len(alert_jobs) == 1
            assert any(
                "appointment is confirmed"
                in decrypt_telegram_secret(item.body_encrypted).casefold()
                for item in history
            )

            async def verify_cross_channel_conflict():
                async with AsyncSessionFactory() as session:
                    with pytest.raises(BookingConflict):
                        await create_appointment_booking(
                            session,
                            tenant_id=tenant_id,
                            service_id=service_id,
                            customer_name="Grace Hopper",
                            customer_phone="+2348098765432",
                            starts_at=appointment.starts_at,
                            conversation_id=uuid4(),
                            booking_attempt_id=uuid4(),
                            source="whatsapp",
                        )

            asyncio.run(verify_cross_channel_conflict())

            async def verify_concurrent_cross_channel_race():
                async with AsyncSessionFactory() as session:
                    slots = await get_available_slots(
                        session,
                        tenant_id=tenant_id,
                        service_id=service_id,
                        day=appointment.starts_at.date(),
                        limit=3,
                    )
                candidate = next(
                    (slot for slot in slots if slot != appointment.starts_at),
                    None,
                )
                assert candidate is not None

                async def attempt(source: str, phone: str) -> str:
                    async with AsyncSessionFactory() as session:
                        try:
                            await create_appointment_booking(
                                session,
                                tenant_id=tenant_id,
                                service_id=service_id,
                                customer_name=f"{source.title()} Customer",
                                customer_phone=phone,
                                starts_at=candidate,
                                conversation_id=uuid4(),
                                booking_attempt_id=uuid4(),
                                source=source,
                            )
                        except BookingConflict:
                            await session.rollback()
                            return "conflict"
                        await session.commit()
                        return "booked"

                outcomes = await asyncio.gather(
                    attempt("telegram", "+2348022222222"),
                    attempt("whatsapp", "+2348033333333"),
                )
                assert sorted(outcomes) == ["booked", "conflict"]

            asyncio.run(verify_concurrent_cross_channel_race())

            if phone_input == "__contact__":
                async def verify_contact_identity():
                    async with AsyncSessionFactory() as session:
                        identity_result = await session.execute(
                            select(TelegramCustomerIdentity).where(
                                TelegramCustomerIdentity.tenant_id == tenant_id,
                                TelegramCustomerIdentity.telegram_user_id == str(user_id),
                                TelegramCustomerIdentity.revoked_at.is_(None),
                            )
                        )
                        return identity_result.scalar_one_or_none()

                assert asyncio.run(verify_contact_identity()) is not None

                new_day = datetime.now(ZoneInfo("Africa/Lagos")).date() + timedelta(days=3)
                management_steps = (
                    (8, "/appointments"),
                    (9, "/reschedule"),
                    (10, "1"),
                    (11, new_day.isoformat()),
                    (12, "1"),
                    (13, "YES"),
                    (14, "/cancel"),
                    (15, "1"),
                    (16, "YES"),
                )
                for sequence, customer_text in management_steps:
                    update_id = send_customer_message(customer_text, sequence)
                    asyncio.run(process_inbound(update_id, sequence))

                async def verify_customer_lifecycle():
                    async with AsyncSessionFactory() as session:
                        appointment_result = await session.execute(
                            select(Appointment).where(
                                Appointment.tenant_id == tenant_id,
                                Appointment.id == appointments[0].id,
                            )
                        )
                        managed_appointment = appointment_result.scalar_one()
                        notification_result = await session.execute(
                            select(TelegramOwnerNotification).where(
                                TelegramOwnerNotification.tenant_id == tenant_id,
                                TelegramOwnerNotification.appointment_id == managed_appointment.id,
                            )
                        )
                        events = {
                            item.event_type
                            for item in notification_result.scalars().all()
                        }
                        return managed_appointment, events

                managed_appointment, lifecycle_events = asyncio.run(
                    verify_customer_lifecycle()
                )
                assert managed_appointment.status == "cancelled"
                assert managed_appointment.starts_at.astimezone(
                    ZoneInfo("Africa/Lagos")
                ).date() == new_day
                assert "appointment.created" in lifecycle_events
                assert "appointment.rescheduled" in lifecycle_events
                assert "appointment.cancelled" in lifecycle_events

    finally:
        if tenant_id is not None:
            async def cleanup():
                async with AsyncSessionFactory() as session:
                    tenant = await session.get(Tenant, tenant_id)
                    if tenant is not None:
                        await session.delete(tenant)
                        await session.commit()

            asyncio.run(cleanup())
        get_settings.cache_clear()
