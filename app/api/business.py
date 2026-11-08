import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import CurrentUser, DbSession, require_roles
from app.models import (
    Appointment,
    AuditEvent,
    BusinessHours,
    Customer,
    Service,
    Tenant,
    User,
)
from app.schemas.business import (
    AppointmentCreate,
    AppointmentReschedule,
    AppointmentResponse,
    BusinessHoursReplace,
    BusinessHoursResponse,
    ServiceCreate,
    ServiceResponse,
)
from app.services.telegram_owner_notifications import enqueue_appointment_owner_alerts

router = APIRouter(tags=["business operations"])
ManagerOrOwner = Annotated[User, Depends(require_roles("owner", "manager"))]


def _service_response(service: Service) -> ServiceResponse:
    return ServiceResponse(
        id=service.id,
        name=service.name,
        description=service.description,
        duration_minutes=service.duration_minutes,
        price_minor=service.price_minor,
        currency=service.currency,
        is_active=service.is_active,
    )


def _appointment_response(appointment: Appointment, customer: Customer) -> AppointmentResponse:
    return AppointmentResponse(
        id=appointment.id,
        service_id=appointment.service_id,
        customer_id=customer.id,
        customer_name=customer.name,
        customer_phone=customer.phone_e164,
        starts_at=appointment.starts_at,
        ends_at=appointment.ends_at,
        status=appointment.status,
        cancelled_at=appointment.cancelled_at,
        completed_at=appointment.completed_at,
        missed_at=appointment.missed_at,
        rescheduled_at=appointment.rescheduled_at,
    )


async def _lock_tenant(session: DbSession, tenant_id: UUID) -> Tenant:
    result = await session.execute(
        select(Tenant).where(Tenant.id == tenant_id, Tenant.status == "active").with_for_update()
    )
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=401, detail="Business account is unavailable")
    return tenant


@router.post("/services", response_model=ServiceResponse, status_code=status.HTTP_201_CREATED)
async def create_service(
    payload: ServiceCreate,
    session: DbSession,
    user: ManagerOrOwner,
) -> ServiceResponse:
    await _lock_tenant(session, user.tenant_id)
    service = Service(
        id=uuid4(),
        tenant_id=user.tenant_id,
        name=payload.name,
        description=payload.description,
        duration_minutes=payload.duration_minutes,
        price_minor=payload.price_minor,
        currency="NGN",
        is_active=True,
    )
    session.add(service)
    session.add(
        AuditEvent(
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            event_type="service.created",
            subject_type="service",
            subject_id=service.id,
            payload={"name": payload.name, "price_minor": payload.price_minor},
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="A service with this name already exists"
        ) from exc
    return _service_response(service)


@router.get("/services", response_model=list[ServiceResponse])
async def list_services(session: DbSession, user: CurrentUser) -> list[ServiceResponse]:
    result = await session.execute(
        select(Service)
        .where(Service.tenant_id == user.tenant_id, Service.is_active.is_(True))
        .order_by(Service.name)
    )
    return [_service_response(service) for service in result.scalars().all()]


@router.put("/business-hours", response_model=list[BusinessHoursResponse])
async def replace_business_hours(
    payload: BusinessHoursReplace,
    session: DbSession,
    user: ManagerOrOwner,
) -> list[BusinessHoursResponse]:
    tenant = await _lock_tenant(session, user.tenant_id)
    await session.execute(delete(BusinessHours).where(BusinessHours.tenant_id == tenant.id))
    rows = [
        BusinessHours(
            tenant_id=tenant.id,
            weekday=day.weekday,
            is_closed=day.is_closed,
            opens_at=day.opens_at,
            closes_at=day.closes_at,
        )
        for day in payload.days
    ]
    session.add_all(rows)
    session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            event_type="business_hours.replaced",
            subject_type="tenant",
            subject_id=tenant.id,
            payload={"configured_weekdays": 7},
        )
    )
    await session.commit()
    return [
        BusinessHoursResponse(
            weekday=row.weekday,
            is_closed=row.is_closed,
            opens_at=row.opens_at,
            closes_at=row.closes_at,
        )
        for row in sorted(rows, key=lambda item: item.weekday)
    ]


@router.get("/business-hours", response_model=list[BusinessHoursResponse])
async def get_business_hours(session: DbSession, user: CurrentUser) -> list[BusinessHoursResponse]:
    result = await session.execute(
        select(BusinessHours)
        .where(BusinessHours.tenant_id == user.tenant_id)
        .order_by(BusinessHours.weekday)
    )
    return [
        BusinessHoursResponse(
            weekday=row.weekday,
            is_closed=row.is_closed,
            opens_at=row.opens_at,
            closes_at=row.closes_at,
        )
        for row in result.scalars().all()
    ]


@router.post(
    "/appointments",
    response_model=AppointmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_appointment(
    payload: AppointmentCreate,
    session: DbSession,
    user: CurrentUser,
) -> AppointmentResponse:
    tenant = await _lock_tenant(session, user.tenant_id)
    starts_at = payload.starts_at.astimezone(UTC)
    fingerprint_payload = {
        "service_id": str(payload.service_id),
        "customer_name": payload.customer_name,
        "customer_phone": payload.customer_phone,
        "starts_at": starts_at.isoformat(),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    existing_result = await session.execute(
        select(Appointment).where(
            Appointment.tenant_id == tenant.id,
            Appointment.idempotency_key == payload.idempotency_key,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key was already used for a different appointment",
            )
        customer_result = await session.execute(
            select(Customer).where(
                Customer.id == existing.customer_id,
                Customer.tenant_id == tenant.id,
            )
        )
        customer = customer_result.scalar_one()
        return _appointment_response(existing, customer)

    service_result = await session.execute(
        select(Service).where(
            Service.id == payload.service_id,
            Service.tenant_id == tenant.id,
            Service.is_active.is_(True),
        )
    )
    service = service_result.scalar_one_or_none()
    if service is None:
        raise HTTPException(status_code=404, detail="Service not found")

    if starts_at <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="Appointment must be in the future")
    ends_at = starts_at + timedelta(minutes=service.duration_minutes)

    business_timezone = ZoneInfo(tenant.business_timezone)
    local_start = starts_at.astimezone(business_timezone)
    local_end = ends_at.astimezone(business_timezone)
    if local_start.date() != local_end.date():
        raise HTTPException(status_code=409, detail="Appointment cannot cross business days")

    hours_result = await session.execute(
        select(BusinessHours).where(
            BusinessHours.tenant_id == tenant.id,
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
        raise HTTPException(status_code=409, detail="Requested time is outside business hours")

    overlap_result = await session.execute(
        select(Appointment.id)
        .where(
            Appointment.tenant_id == tenant.id,
            Appointment.status == "confirmed",
            Appointment.starts_at < ends_at,
            Appointment.ends_at > starts_at,
        )
        .limit(1)
    )
    if overlap_result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Requested time is no longer available")

    customer_result = await session.execute(
        select(Customer).where(
            Customer.tenant_id == tenant.id,
            Customer.phone_e164 == payload.customer_phone,
        )
    )
    customer = customer_result.scalar_one_or_none()
    if customer is None:
        customer = Customer(
            tenant_id=tenant.id,
            name=payload.customer_name,
            phone_e164=payload.customer_phone,
        )
        session.add(customer)
        await session.flush()

    appointment = Appointment(
        id=uuid4(),
        tenant_id=tenant.id,
        idempotency_key=payload.idempotency_key,
        request_fingerprint=fingerprint,
        service_id=service.id,
        customer_id=customer.id,
        created_by_user_id=user.id,
        starts_at=starts_at,
        ends_at=ends_at,
        status="confirmed",
    )
    session.add(appointment)
    session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            event_type="appointment.created",
            subject_type="appointment",
            subject_id=appointment.id,
            payload={
                "service_id": str(service.id),
                "customer_id": str(customer.id),
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat(),
            },
        )
    )
    await session.flush()
    await enqueue_appointment_owner_alerts(
        session,
        tenant_id=tenant.id,
        appointment_id=appointment.id,
    )

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="Appointment could not be confirmed"
        ) from exc

    return _appointment_response(appointment, customer)


@router.get("/appointments", response_model=list[AppointmentResponse])
async def list_appointments(
    session: DbSession,
    user: CurrentUser,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[AppointmentResponse]:
    result = await session.execute(
        select(Appointment, Customer)
        .join(Customer, Customer.id == Appointment.customer_id)
        .where(Appointment.tenant_id == user.tenant_id)
        .order_by(Appointment.starts_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return [
        _appointment_response(appointment, customer)
        for appointment, customer in result.all()
    ]


@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentResponse)
async def cancel_appointment(
    appointment_id: UUID,
    session: DbSession,
    user: CurrentUser,
) -> AppointmentResponse:
    tenant = await _lock_tenant(session, user.tenant_id)
    result = await session.execute(
        select(Appointment, Customer)
        .join(Customer, Customer.id == Appointment.customer_id)
        .where(
            Appointment.id == appointment_id,
            Appointment.tenant_id == tenant.id,
        )
        .with_for_update(of=Appointment)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Appointment not found")
    appointment, customer = row
    if appointment.status != "confirmed":
        raise HTTPException(status_code=409, detail="Appointment is not cancellable")

    now = datetime.now(UTC)
    appointment.status = "cancelled"
    appointment.cancelled_at = now
    session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            event_type="appointment.cancelled",
            subject_type="appointment",
            subject_id=appointment.id,
            payload={"cancelled_at": now.isoformat()},
        )
    )
    await session.commit()
    return _appointment_response(appointment, customer)


async def _get_locked_appointment(
    session: DbSession,
    tenant_id: UUID,
    appointment_id: UUID,
) -> tuple[Appointment, Customer]:
    result = await session.execute(
        select(Appointment, Customer)
        .join(Customer, Customer.id == Appointment.customer_id)
        .where(
            Appointment.id == appointment_id,
            Appointment.tenant_id == tenant_id,
        )
        .with_for_update(of=Appointment)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return row


@router.get("/appointments/{appointment_id}", response_model=AppointmentResponse)
async def get_appointment(
    appointment_id: UUID,
    session: DbSession,
    user: CurrentUser,
) -> AppointmentResponse:
    result = await session.execute(
        select(Appointment, Customer)
        .join(Customer, Customer.id == Appointment.customer_id)
        .where(
            Appointment.id == appointment_id,
            Appointment.tenant_id == user.tenant_id,
        )
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Appointment not found")
    appointment, customer = row
    return _appointment_response(appointment, customer)


@router.post("/appointments/{appointment_id}/complete", response_model=AppointmentResponse)
async def complete_appointment(
    appointment_id: UUID,
    session: DbSession,
    user: ManagerOrOwner,
) -> AppointmentResponse:
    tenant = await _lock_tenant(session, user.tenant_id)
    appointment, customer = await _get_locked_appointment(session, tenant.id, appointment_id)
    if appointment.status != "confirmed":
        raise HTTPException(status_code=409, detail="Only confirmed appointments can be completed")
    now = datetime.now(UTC)
    appointment.status = "completed"
    appointment.completed_at = now
    session.add(AuditEvent(
        tenant_id=tenant.id,
        actor_user_id=user.id,
        event_type="appointment.completed",
        subject_type="appointment",
        subject_id=appointment.id,
        payload={"completed_at": now.isoformat()},
    ))
    await session.commit()
    return _appointment_response(appointment, customer)


@router.post("/appointments/{appointment_id}/missed", response_model=AppointmentResponse)
async def mark_appointment_missed(
    appointment_id: UUID,
    session: DbSession,
    user: ManagerOrOwner,
) -> AppointmentResponse:
    tenant = await _lock_tenant(session, user.tenant_id)
    appointment, customer = await _get_locked_appointment(session, tenant.id, appointment_id)
    if appointment.status != "confirmed":
        raise HTTPException(status_code=409, detail="Only confirmed appointments can be marked missed")
    now = datetime.now(UTC)
    appointment.status = "missed"
    appointment.missed_at = now
    session.add(AuditEvent(
        tenant_id=tenant.id,
        actor_user_id=user.id,
        event_type="appointment.missed",
        subject_type="appointment",
        subject_id=appointment.id,
        payload={"missed_at": now.isoformat()},
    ))
    await session.commit()
    return _appointment_response(appointment, customer)


@router.post("/appointments/{appointment_id}/reschedule", response_model=AppointmentResponse)
async def reschedule_appointment(
    appointment_id: UUID,
    payload: AppointmentReschedule,
    session: DbSession,
    user: ManagerOrOwner,
) -> AppointmentResponse:
    tenant = await _lock_tenant(session, user.tenant_id)
    appointment, customer = await _get_locked_appointment(session, tenant.id, appointment_id)
    if appointment.status != "confirmed":
        raise HTTPException(status_code=409, detail="Only confirmed appointments can be rescheduled")

    new_start = payload.starts_at.astimezone(UTC)
    if new_start == appointment.starts_at:
        return _appointment_response(appointment, customer)
    if new_start <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="Appointment must be in the future")

    service_result = await session.execute(
        select(Service).where(Service.id == appointment.service_id, Service.tenant_id == tenant.id)
    )
    service = service_result.scalar_one_or_none()
    if service is None:
        raise HTTPException(status_code=409, detail="Appointment service is unavailable")

    new_end = new_start + timedelta(minutes=service.duration_minutes)
    business_timezone = ZoneInfo(tenant.business_timezone)
    local_start = new_start.astimezone(business_timezone)
    local_end = new_end.astimezone(business_timezone)
    if local_start.date() != local_end.date():
        raise HTTPException(status_code=409, detail="Appointment cannot cross business days")

    hours_result = await session.execute(
        select(BusinessHours).where(
            BusinessHours.tenant_id == tenant.id,
            BusinessHours.weekday == local_start.weekday(),
        )
    )
    hours = hours_result.scalar_one_or_none()
    local_start_time = local_start.time().replace(tzinfo=None)
    local_end_time = local_end.time().replace(tzinfo=None)
    if (
        hours is None or hours.is_closed or hours.opens_at is None or hours.closes_at is None
        or local_start_time < hours.opens_at or local_end_time > hours.closes_at
    ):
        raise HTTPException(status_code=409, detail="Requested time is outside business hours")

    overlap_result = await session.execute(
        select(Appointment.id).where(
            Appointment.tenant_id == tenant.id,
            Appointment.id != appointment.id,
            Appointment.status == "confirmed",
            Appointment.starts_at < new_end,
            Appointment.ends_at > new_start,
        ).limit(1)
    )
    if overlap_result.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Requested time is no longer available")

    old_start, old_end = appointment.starts_at, appointment.ends_at
    now = datetime.now(UTC)
    appointment.starts_at = new_start
    appointment.ends_at = new_end
    appointment.rescheduled_at = now
    session.add(AuditEvent(
        tenant_id=tenant.id,
        actor_user_id=user.id,
        event_type="appointment.rescheduled",
        subject_type="appointment",
        subject_id=appointment.id,
        payload={
            "previous_starts_at": old_start.isoformat(),
            "previous_ends_at": old_end.isoformat(),
            "starts_at": new_start.isoformat(),
            "ends_at": new_end.isoformat(),
            "rescheduled_at": now.isoformat(),
        },
    ))
    await session.commit()
    return _appointment_response(appointment, customer)
