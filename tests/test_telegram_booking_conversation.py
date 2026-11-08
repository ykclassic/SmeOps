import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.telegram import decrypt_telegram_secret
from app.db.session import AsyncSessionFactory
from app.jobs.queue import ClaimedJob
from app.jobs.telegram import accept_inbound_telegram_job
from app.main import app
from app.models import (
    Appointment,
    BackgroundJob,
    Customer,
    TelegramConversationMessage,
    TelegramOutboundMessage,
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

    async def fake_get_bot_identity(*, bot_token):
        assert bot_token == "123456:telegram-test-token"
        return type("Identity", (), {"bot_id": "123456789", "username": "smeops_test_bot"})()

    async def fake_register_webhook(_connection):
        return None

    monkeypatch.setattr(telegram_api, "get_bot_identity", fake_get_bot_identity)
    monkeypatch.setattr(telegram_api, "_register_connection_webhook", fake_register_webhook)

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

            provision = client.post(
                "/telegram/connection",
                headers=auth,
                json={"bot_token": "123456:telegram-test-token"},
            )
            assert provision.status_code == 202
            assert provision.json()["status"] == "active"

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
                connection_secret = telegram_api._telegram_webhook_secret("123456789")
                response = client.post(
                    "/webhooks/telegram/123456789",
                    json=payload,
                    headers={"X-Telegram-Bot-Api-Secret-Token": connection_secret},
                )
                assert response.status_code == 200, response.text
                assert response.json()["queued_events"] == 1
                return str(update_id)

            async def process_inbound(update_id: str, sequence: int) -> None:
                inbound_job = await find_job(
                    "telegram.accept_inbound",
                    f"tg:update:{update_id}",
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

            steps = ("BOOK", "1", "Ada Lovelace", phone_input, "tomorrow", "1", "YES")
            for sequence, customer_text in enumerate(steps, start=1):
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
                    return appointments, outbound, history, customers

            appointments, outbound, history, customers = asyncio.run(inspect_results())
            assert len(appointments) == 1
            appointment = appointments[0]
            assert appointment.status == "confirmed"
            assert len(outbound) == 7
            assert len(history) == 7
            assert len(customers) == 1
            assert customers[0].phone_e164 == expected_phone
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
