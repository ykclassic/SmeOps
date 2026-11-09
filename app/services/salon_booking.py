import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Appointment,
    AuditEvent,
    BusinessHours,
    Customer,
    Service,
    Tenant,
    User,
)
from app.services.telegram_owner_notifications import enqueue_appointment_owner_alerts

SLOT_INTERVAL_MINUTES = 30
BOOKING_NAMESPACE = UUID("a2a42f47-2ed0-47e6-86cb-1c4ef4b2d129")


class BookingConflict(ValueError):
    """A requested appointment cannot be booked."""


async def get_available_slots(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    service_id: UUID,
    day: date,
    limit: int = 3,
    exclude_appointment_id: UUID | None = None,
) -> list[datetime]:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or tenant.status != "active":
        raise BookingConflict("Business is unavailable")
    service_result = await session.execute(
        select(Service).where(
            Service.id == service_id,
            Service.tenant_id == tenant_id,
            Service.is_active.is_(True),
        )
    )
    service = service_result.scalar_one_or_none()
    if service is None:
        raise BookingConflict("Service is unavailable")

    timezone = ZoneInfo(tenant.business_timezone)
    hours_result = await session.execute(
        select(BusinessHours).where(
            BusinessHours.tenant_id == tenant_id,
            BusinessHours.weekday == day.weekday(),
        )
    )
    hours = hours_result.scalar_one_or_none()
    if (
        hours is None
        or hours.is_closed
        or hours.opens_at is None
        or hours.closes_at is None
    ):
        return []

    local_open = datetime.combine(day, hours.opens_at, tzinfo=timezone)
    local_close = datetime.combine(day, hours.closes_at, tzinfo=timezone)
    duration = timedelta(minutes=service.duration_minutes)
    now = datetime.now(UTC)

    existing_result = await session.execute(
        select(Appointment.starts_at, Appointment.ends_at).where(
            Appointment.tenant_id == tenant_id,
            Appointment.status == "confirmed",
            Appointment.id != exclude_appointment_id if exclude_appointment_id else True,
            Appointment.starts_at < local_close.astimezone(UTC),
            Appointment.ends_at > local_open.astimezone(UTC),
        )
    )
    occupied = list(existing_result.all())

    available: list[datetime] = []
    cursor = local_open
    while cursor + duration <= local_close and len(available) < limit:
        start_utc = cursor.astimezone(UTC)
        end_utc = (cursor + duration).astimezone(UTC)
        if start_utc > now and not any(
            start_utc < existing_end and end_utc > existing_start
            for existing_start, existing_end in occupied
        ):
            available.append(start_utc)
        cursor += timedelta(minutes=SLOT_INTERVAL_MINUTES)
    return available


async def create_appointment_booking(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    service_id: UUID,
    customer_name: str,
    customer_phone: str,
    starts_at: datetime,
    conversation_id: UUID,
    booking_attempt_id: UUID,
    source: str = "whatsapp",
) -> Appointment:
    tenant_result = await session.execute(
        select(Tenant)
        .where(Tenant.id == tenant_id, Tenant.status == "active")
        .with_for_update()
    )
    tenant = tenant_result.scalar_one_or_none()
    if tenant is None:
        raise BookingConflict("Business is unavailable")

    starts_at = starts_at.astimezone(UTC)
    idempotency_key = uuid5(
        BOOKING_NAMESPACE,
        f"{conversation_id}:{booking_attempt_id}",
    )
    fingerprint_payload = {
        "service_id": str(service_id),
        "customer_name": customer_name,
        "customer_phone": customer_phone,
        "starts_at": starts_at.isoformat(),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    existing_result = await session.execute(
        select(Appointment).where(
            Appointment.tenant_id == tenant_id,
            Appointment.idempotency_key == idempotency_key,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise BookingConflict("Booking request conflicts with an earlier request")
        return existing

    service_result = await session.execute(
        select(Service).where(
            Service.id == service_id,
            Service.tenant_id == tenant_id,
            Service.is_active.is_(True),
        )
    )
    service = service_result.scalar_one_or_none()
    if service is None:
        raise BookingConflict("That service is no longer available")

    ends_at = starts_at + timedelta(minutes=service.duration_minutes)
    timezone = ZoneInfo(tenant.business_timezone)
    local_start = starts_at.astimezone(timezone)
    local_end = ends_at.astimezone(timezone)
    if local_start.date() != local_end.date() or starts_at <= datetime.now(UTC):
        raise BookingConflict("That appointment time is no longer valid")

    hours_result = await session.execute(
        select(BusinessHours).where(
            BusinessHours.tenant_id == tenant_id,
            BusinessHours.weekday == local_start.weekday(),
        )
    )
    hours = hours_result.scalar_one_or_none()
    local_start_time = local_start.time().replace(tzinfo=None)
    local_end_time = local_end.time().replace(tzinfo=None)
    if (
        hours is None
        or hours.is_closed
        or hours.opens_at is None
        or hours.closes_at is None
        or local_start_time < hours.opens_at
        or local_end_time > hours.closes_at
    ):
        raise BookingConflict("That time is outside the salon's opening hours")

    overlap_result = await session.execute(
        select(Appointment.id)
        .where(
            Appointment.tenant_id == tenant_id,
            Appointment.status == "confirmed",
            Appointment.starts_at < ends_at,
            Appointment.ends_at > starts_at,
        )
        .limit(1)
    )
    if overlap_result.scalar_one_or_none() is not None:
        raise BookingConflict("That time has just been booked. Please choose another slot")

    customer_result = await session.execute(
        select(Customer).where(
            Customer.tenant_id == tenant_id,
            Customer.phone_e164 == customer_phone,
        )
    )
    customer = customer_result.scalar_one_or_none()
    if customer is None:
        customer = Customer(
            tenant_id=tenant_id,
            name=customer_name,
            phone_e164=customer_phone,
        )
        session.add(customer)
        await session.flush()
    else:
        customer.name = customer_name

    owner_result = await session.execute(
        select(User)
        .where(
            User.tenant_id == tenant_id,
            User.role == "owner",
            User.status == "active",
        )
        .order_by(User.created_at)
        .limit(1)
    )
    owner = owner_result.scalar_one_or_none()
    if owner is None:
        raise BookingConflict("The salon account has no active owner")

    appointment = Appointment(
        id=uuid4(),
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        service_id=service_id,
        customer_id=customer.id,
        created_by_user_id=owner.id,
        starts_at=starts_at,
        ends_at=ends_at,
        status="confirmed",
    )
    session.add(appointment)
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_user_id=None,
            event_type="appointment.created",
            subject_type="appointment",
            subject_id=appointment.id,
            payload={
                "source": source,
                "conversation_id": str(conversation_id),
                "service_id": str(service_id),
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat(),
            },
        )
    )
    await session.flush()
    await enqueue_appointment_owner_alerts(
        session,
        tenant_id=tenant_id,
        appointment_id=appointment.id,
    )
    return appointment


def parse_booking_date(value: str, *, timezone: ZoneInfo, now: datetime) -> date | None:
    normalized = value.strip().casefold()
    local_today = now.astimezone(timezone).date()
    if normalized in {"today", "tonight"}:
        return local_today
    if normalized == "tomorrow":
        return local_today + timedelta(days=1)
    try:
        parsed = date.fromisoformat(normalized)
    except ValueError:
        parsed = None
    if parsed is not None:
        return parsed
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", normalized)
    if match:
        try:
            return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
        except ValueError:
            return None
    weekdays = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    target = weekdays.get(normalized)
    if target is not None:
        days_ahead = (target - local_today.weekday()) % 7
        return local_today + timedelta(days=days_ahead or 7)
    return None


# Backwards-compatible entry point for existing WhatsApp callers and tests.
create_whatsapp_booking = create_appointment_booking
