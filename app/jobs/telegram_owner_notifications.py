import logging
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.core.telegram import decrypt_telegram_secret
from app.db.session import AsyncSessionFactory
from app.integrations.telegram_bot import (
    TelegramAPIError,
    TelegramDeliveryUnknown,
    TelegramSendRejected,
    send_text_message,
)
from app.jobs.queue import ClaimedJob
from app.models import (
    Appointment,
    Customer,
    Service,
    TelegramConnection,
    TelegramOwnerLink,
    TelegramOwnerNotification,
    Tenant,
    User,
)

logger = logging.getLogger(__name__)


async def send_owner_appointment_alert_job(job: ClaimedJob) -> None:
    try:
        notification_id = UUID(job.payload["notification_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid owner notification job payload") from exc

    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(TelegramOwnerNotification)
            .where(
                TelegramOwnerNotification.id == notification_id,
                TelegramOwnerNotification.tenant_id == job.tenant_id,
            )
            .with_for_update()
        )
        notification = result.scalar_one_or_none()
        if notification is None:
            raise ValueError("Owner notification does not exist")
        if notification.status in {"sent", "failed", "unknown"}:
            return
        if notification.status == "sending":
            notification.status = "unknown"
            notification.last_error_code = "previous_send_outcome_unknown"
            await session.commit()
            return
        if notification.status != "queued":
            raise ValueError("Owner notification is not sendable")

        link_result = await session.execute(
            select(TelegramOwnerLink)
            .join(
                User,
                (User.id == TelegramOwnerLink.owner_user_id)
                & (User.tenant_id == TelegramOwnerLink.tenant_id),
            )
            .where(
                User.status == "active",
                User.role == "owner",
                TelegramOwnerLink.tenant_id == job.tenant_id,
                TelegramOwnerLink.owner_user_id == notification.owner_user_id,
                TelegramOwnerLink.linked_at.is_not(None),
                TelegramOwnerLink.disabled_at.is_(None),
                TelegramOwnerLink.chat_id.is_not(None),
            )
        )
        link = link_result.scalar_one_or_none()
        connection_result = await session.execute(
            select(TelegramConnection).where(
                TelegramConnection.tenant_id == job.tenant_id,
                TelegramConnection.status == "active",
            )
        )
        connection = connection_result.scalar_one_or_none()
        appointment_result = await session.execute(
            select(Appointment, Service, Customer, Tenant)
            .join(
                Service,
                (Service.id == Appointment.service_id)
                & (Service.tenant_id == Appointment.tenant_id),
            )
            .join(
                Customer,
                (Customer.id == Appointment.customer_id)
                & (Customer.tenant_id == Appointment.tenant_id),
            )
            .join(Tenant, Tenant.id == Appointment.tenant_id)
            .where(
                Appointment.id == notification.appointment_id,
                Appointment.tenant_id == job.tenant_id,
                Appointment.status == "confirmed",
            )
        )
        appointment_row = appointment_result.one_or_none()

        if link is None or connection is None or appointment_row is None:
            notification.status = "failed"
            notification.last_error_code = "recipient_or_appointment_unavailable"
            await session.commit()
            return

        appointment, service, customer, tenant = appointment_row
        timezone = ZoneInfo(tenant.business_timezone)
        local_start = appointment.starts_at.astimezone(timezone)
        reference = str(appointment.id).replace("-", "")[:8].upper()
        body = (
            "NEW APPOINTMENT CONFIRMED\n\n"
            f"Business: {tenant.name}\n"
            f"Reference: {reference}\n"
            f"Service: {service.name}\n"
            f"Customer: {customer.name}\n"
            f"Date: {local_start.strftime('%A, %d %B %Y')}\n"
            f"Time: {local_start.strftime('%I:%M %p')}\n"
            "Status: Confirmed\n\n"
            "Open SmeOps to manage this appointment."
        )
        bot_token = decrypt_telegram_secret(connection.bot_token_encrypted)
        chat_id = link.chat_id
        notification.status = "sending"
        await session.commit()

    try:
        sent = await send_text_message(
            bot_token=bot_token,
            chat_id=chat_id,
            body=body,
        )
    except (TelegramAPIError, TelegramSendRejected):
        status_value = "failed"
        error_code = "provider_rejected"
        provider_message_id = None
    except TelegramDeliveryUnknown:
        status_value = "unknown"
        error_code = "provider_outcome_unknown"
        provider_message_id = None
    else:
        status_value = "sent"
        error_code = None
        provider_message_id = f"{chat_id}:{sent.provider_message_id}"

    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(TelegramOwnerNotification)
            .where(
                TelegramOwnerNotification.id == notification_id,
                TelegramOwnerNotification.tenant_id == job.tenant_id,
            )
            .with_for_update()
        )
        notification = result.scalar_one_or_none()
        if notification is None or notification.status != "sending":
            logger.warning(
                "Owner notification state changed while sending",
                extra={"notification_id": str(notification_id)},
            )
            return
        notification.status = status_value
        notification.last_error_code = error_code
        notification.provider_message_id = provider_message_id
        if status_value == "sent":
            notification.sent_at = datetime.now(UTC)
        await session.commit()
