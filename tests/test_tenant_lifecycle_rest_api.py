import asyncio
from datetime import datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text, update

from app.db.session import AsyncSessionFactory
from app.main import app
from app.models import (
    Appointment,
    AuditEvent,
    BackgroundJob,
    BusinessHours,
    Customer,
    Order,
    OrderItem,
    Product,
    Service,
    TelegramOutboundMessage,
    TelegramOwnerNotification,
    Tenant,
    User,
    WhatsAppOutboundMessage,
)
from app.services.tenant_lifecycle import transition_tenant_lifecycle

PASSWORD = "Correct-Horse-Battery-42"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register(client: TestClient) -> tuple[str, str]:
    slug = f"adm011-{uuid4().hex[:16]}"
    response = client.post(
        "/auth/register",
        json={
            "business_name": "ADM-011 REST lifecycle test",
            "tenant_slug": slug,
            "email": f"{slug}@example.com",
            "password": PASSWORD,
            "business_timezone": "Africa/Lagos",
        },
    )
    assert response.status_code == 201
    return response.json()["access_token"], slug


def _set_status(tenant_id: str, value: str) -> None:
    async def update_status() -> None:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                await session.execute(
                    update(Tenant)
                    .where(Tenant.id == UUID(tenant_id))
                    .values(status=value)
                )

    asyncio.run(update_status())


def _tenant_snapshot(tenant_id: str) -> dict[str, int]:
    models = (
        Appointment,
        Order,
        OrderItem,
        Product,
        Customer,
        Service,
        BusinessHours,
        AuditEvent,
        BackgroundJob,
        TelegramOutboundMessage,
        TelegramOwnerNotification,
        WhatsAppOutboundMessage,
    )

    async def read() -> dict[str, int]:
        async with AsyncSessionFactory() as session:
            counts = {}
            for model in models:
                counts[model.__tablename__] = await session.scalar(
                    select(func.count()).select_from(model).where(
                        model.tenant_id == UUID(tenant_id)
                    )
                )
            for model, states in (
                (Appointment, ("confirmed", "cancelled", "completed", "missed")),
                (Order, ("pending_approval", "approved", "cancelled", "fulfilled")),
            ):
                for state in states:
                    counts[f"{model.__tablename__}:{state}"] = await session.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(
                            model.tenant_id == UUID(tenant_id),
                            model.status == state,
                        )
                    )
            counts["product_stock_total"] = await session.scalar(
                select(func.coalesce(func.sum(Product.stock_quantity), 0)).where(
                    Product.tenant_id == UUID(tenant_id)
                )
            )
            return counts

    return asyncio.run(read())


def _cleanup(tenant_id: str) -> None:
    async def cleanup() -> None:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                # Remove rows with actor/creator references before users.
                await session.execute(
                    delete(AuditEvent).where(AuditEvent.tenant_id == UUID(tenant_id))
                )
                await session.execute(
                    delete(TelegramOutboundMessage).where(
                        TelegramOutboundMessage.tenant_id == UUID(tenant_id)
                    )
                )
                await session.execute(
                    delete(TelegramOwnerNotification).where(
                        TelegramOwnerNotification.tenant_id == UUID(tenant_id)
                    )
                )
                await session.execute(
                    delete(WhatsAppOutboundMessage).where(
                        WhatsAppOutboundMessage.tenant_id == UUID(tenant_id)
                    )
                )
                await session.execute(
                    delete(BackgroundJob).where(
                        BackgroundJob.tenant_id == UUID(tenant_id)
                    )
                )
                await session.execute(
                    delete(Appointment).where(
                        Appointment.tenant_id == UUID(tenant_id)
                    )
                )
                await session.execute(
                    delete(OrderItem).where(
                        OrderItem.tenant_id == UUID(tenant_id)
                    )
                )
                await session.execute(
                    delete(Order).where(
                        Order.tenant_id == UUID(tenant_id)
                    )
                )
                await session.execute(
                    delete(BusinessHours).where(
                        BusinessHours.tenant_id == UUID(tenant_id)
                    )
                )
                await session.execute(
                    delete(Product).where(
                        Product.tenant_id == UUID(tenant_id)
                    )
                )
                await session.execute(
                    delete(Service).where(
                        Service.tenant_id == UUID(tenant_id)
                    )
                )
                await session.execute(
                    delete(Customer).where(
                        Customer.tenant_id == UUID(tenant_id)
                    )
                )
                await session.execute(
                    delete(User).where(
                        User.tenant_id == UUID(tenant_id)
                    )
                )
                await session.execute(
                    delete(Tenant).where(
                        Tenant.id == UUID(tenant_id)
                    )
                )

    asyncio.run(cleanup())


def _seed_business_and_retail(client: TestClient, token: str) -> tuple[dict, dict, dict]:
    headers = _headers(token)
    service_response = client.post(
        "/services",
        headers=headers,
        json={
            "name": "ADM-011 service",
            "duration_minutes": 30,
            "price_minor": 250000,
        },
    )
    assert service_response.status_code == 201
    service_id = service_response.json()["id"]

    hours_response = client.put(
        "/business-hours",
        headers=headers,
        json={
            "days": [
                {
                    "weekday": day,
                    "is_closed": False,
                    "opens_at": "08:00:00",
                    "closes_at": "18:00:00",
                }
                for day in range(7)
            ]
        },
    )
    assert hours_response.status_code == 200

    starts_at = (datetime.now(ZoneInfo("Africa/Lagos")) + timedelta(days=3)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    appointment_payload = {
        "idempotency_key": str(uuid4()),
        "service_id": service_id,
        "customer_name": "ADM-011 Customer",
        "customer_phone": "+2348012345678",
        "starts_at": starts_at.isoformat(),
    }
    appointment_response = client.post(
        "/appointments", headers=headers, json=appointment_payload
    )
    assert appointment_response.status_code == 201

    product_response = client.post(
        "/products",
        headers=headers,
        json={
            "sku": f"ADM011-{uuid4().hex[:8]}",
            "name": "ADM-011 inventory",
            "price_minor": 100000,
            "stock_quantity": 10,
        },
    )
    assert product_response.status_code == 201
    product_id = product_response.json()["id"]

    order_payload = {
        "idempotency_key": str(uuid4()),
        "customer_name": "ADM-011 Retail Customer",
        "customer_phone": "+2348098765432",
        "items": [{"product_id": product_id, "quantity": 1}],
    }
    order_response = client.post("/orders", headers=headers, json=order_payload)
    assert order_response.status_code == 201

    return (
        {
            "id": appointment_response.json()["id"],
            "payload": appointment_payload,
            "reschedule": {
                "starts_at": (starts_at + timedelta(hours=1)).isoformat()
            },
        },
        {"id": order_response.json()["id"], "payload": order_payload},
        {"id": product_id},
    )


def _mutation_requests(client: TestClient, token: str, seeded: tuple[dict, dict, dict]):
    headers = _headers(token)
    appointment, order, product = seeded
    new_start = (
        datetime.now(ZoneInfo("Africa/Lagos")) + timedelta(days=5)
    ).replace(hour=11, minute=0, second=0, microsecond=0)
    new_appointment = {
        "idempotency_key": str(uuid4()),
        "service_id": appointment["payload"]["service_id"],
        "customer_name": "New ADM-011 Customer",
        "customer_phone": "+2348012345688",
        "starts_at": new_start.isoformat(),
    }
    new_order = {
        "idempotency_key": str(uuid4()),
        "customer_name": "New ADM-011 Retail Customer",
        "customer_phone": "+2348098765433",
        "items": [{"product_id": product["id"], "quantity": 1}],
    }
    return [
        client.post("/services", headers=headers, json={
            "name": f"Mutation service {uuid4().hex[:6]}",
            "duration_minutes": 30,
            "price_minor": 1000,
        }),
        client.put("/business-hours", headers=headers, json={
            "days": [
                {
                    "weekday": day,
                    "is_closed": False,
                    "opens_at": "08:00:00",
                    "closes_at": "18:00:00",
                }
                for day in range(7)
            ]
        }),
        client.post("/appointments", headers=headers, json=new_appointment),
        # These exact idempotency replays must be rejected before returning the
        # previously created appointment/order for a non-active tenant.
        client.post("/appointments", headers=headers, json=appointment["payload"]),
        client.post(f"/appointments/{appointment['id']}/cancel", headers=headers),
        client.post(f"/appointments/{appointment['id']}/complete", headers=headers),
        client.post(f"/appointments/{appointment['id']}/missed", headers=headers),
        client.post(
            f"/appointments/{appointment['id']}/reschedule",
            headers=headers,
            json=appointment["reschedule"],
        ),
        client.post("/products", headers=headers, json={
            "sku": f"NEW-{uuid4().hex[:8]}",
            "name": "New ADM-011 product",
            "price_minor": 2000,
            "stock_quantity": 3,
        }),
        client.post("/orders", headers=headers, json=new_order),
        client.post("/orders", headers=headers, json=order["payload"]),
        client.post(f"/orders/{order['id']}/approve", headers=headers),
        client.post(f"/orders/{order['id']}/cancel", headers=headers),
        client.post(f"/orders/{order['id']}/fulfill", headers=headers),
    ]


@pytest.mark.parametrize("tenant_status", ["active", "deactivated", "suspended", "pending"])
def test_rest_mutation_routes_enforce_current_tenant_state_and_preserve_side_effects(
    tenant_status: str,
):
    with TestClient(app) as client:
        token, _ = _register(client)
        identity = client.get("/auth/me", headers=_headers(token))
        assert identity.status_code == 200
        tenant_id = identity.json()["tenant_id"]
        seeded = _seed_business_and_retail(client, token)

        _set_status(tenant_id, tenant_status)
        before = _tenant_snapshot(tenant_id)
        responses = _mutation_requests(client, token, seeded)

    try:
        if tenant_status == "active":
            expected_statuses = [
                201, 200, 201, 200, 200, 409, 409, 409,
                201, 201, 200, 200, 409, 409,
            ]
            assert [response.status_code for response in responses] == expected_statuses
        else:
            assert [response.status_code for response in responses] == [401] * len(responses)
            assert _tenant_snapshot(tenant_id) == before
    finally:
        _cleanup(tenant_id)


@pytest.mark.parametrize("tenant_status", ["active", "deactivated", "suspended", "pending"])
def test_read_only_rest_routes_follow_existing_current_user_authentication_contract(
    tenant_status: str,
):
    with TestClient(app) as client:
        token, _ = _register(client)
        tenant_id = client.get("/auth/me", headers=_headers(token)).json()["tenant_id"]
        seeded = _seed_business_and_retail(client, token)
        appointment_id = seeded[0]["id"]

        _set_status(tenant_id, tenant_status)
        responses = [
            client.get("/services", headers=_headers(token)),
            client.get("/business-hours", headers=_headers(token)),
            client.get("/appointments", headers=_headers(token)),
            client.get(f"/appointments/{appointment_id}", headers=_headers(token)),
            client.get("/products", headers=_headers(token)),
            client.get("/orders", headers=_headers(token)),
        ]

    try:
        expected = 200 if tenant_status == "active" else 401
        assert [response.status_code for response in responses] == [expected] * len(responses)
    finally:
        _cleanup(tenant_id)


async def _wait_for_blocker(waiting_pid: int, blocking_pid: int) -> None:
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


@pytest.mark.parametrize("operation", ["appointment_create", "retail_order_create"])
async def test_rest_create_operation_waits_for_deactivation_tenant_lock(operation: str):
    from app.schemas.business import AppointmentCreate
    from app.schemas.retail import OrderCreate, OrderItemCreate

    tenant_id = uuid4()
    owner_id = uuid4()
    service_id = uuid4()
    product_id = uuid4()
    local_start = (datetime.now(ZoneInfo("Africa/Lagos")) + timedelta(days=3)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    async with AsyncSessionFactory() as setup:
        async with setup.begin():
            setup.add(Tenant(
                id=tenant_id,
                name="ADM-011 REST lock test",
                slug=f"adm011-lock-{uuid4().hex}",
                status="active",
                business_timezone="Africa/Lagos",
            ))
            setup.add(User(
                id=owner_id,
                tenant_id=tenant_id,
                email=f"{owner_id.hex}@example.test",
                password_hash="test-only-hash",
                role="owner",
                status="active",
            ))
            setup.add(Service(
                id=service_id,
                tenant_id=tenant_id,
                name="ADM-011 lock service",
                duration_minutes=30,
                price_minor=1000,
                currency="NGN",
                is_active=True,
            ))
            setup.add(BusinessHours(
                tenant_id=tenant_id,
                weekday=local_start.weekday(),
                is_closed=False,
                opens_at=time(8, 0),
                closes_at=time(18, 0),
            ))
            setup.add(Product(
                id=product_id,
                tenant_id=tenant_id,
                sku=f"LOCK-{uuid4().hex[:8]}",
                name="ADM-011 lock product",
                price_minor=1000,
                stock_quantity=5,
                is_active=True,
            ))

    try:
        async with AsyncSessionFactory() as lifecycle_session:
            async with lifecycle_session.begin():
                lifecycle_pid = await lifecycle_session.scalar(text("SELECT pg_backend_pid()"))
                await transition_tenant_lifecycle(
                    lifecycle_session,
                    tenant_id=tenant_id,
                    actor_user_id=owner_id,
                    target_status="deactivated",
                )
                started = asyncio.Event()
                request_pid = None

                async def invoke_rest_operation():
                    nonlocal request_pid
                    async with AsyncSessionFactory() as request_session:
                        request_pid = await request_session.scalar(
                            text("SELECT pg_backend_pid()")
                        )
                        user = await request_session.get(User, owner_id)
                        assert user is not None
                        started.set()
                        if operation == "appointment_create":
                            from app.api.business import create_appointment

                            payload = AppointmentCreate(
                                idempotency_key=uuid4(),
                                service_id=service_id,
                                customer_name="REST lock customer",
                                customer_phone="+2348012345678",
                                starts_at=local_start,
                            )
                            return await create_appointment(payload, request_session, user)

                        from app.api.retail import create_order

                        payload = OrderCreate(
                            idempotency_key=uuid4(),
                            customer_name="REST lock customer",
                            customer_phone="+2348098765432",
                            items=[OrderItemCreate(product_id=product_id, quantity=1)],
                        )
                        return await create_order(payload, request_session, user)

                task = asyncio.create_task(invoke_rest_operation())
                await asyncio.wait_for(started.wait(), timeout=2)
                assert request_pid is not None
                await _wait_for_blocker(request_pid, lifecycle_pid)
                assert not task.done()

            with pytest.raises(HTTPException) as raised:
                await task
            assert raised.value.status_code == 401

        async with AsyncSessionFactory() as verify:
            assert await verify.scalar(
                select(func.count(Appointment.id)).where(Appointment.tenant_id == tenant_id)
            ) == 0
            assert await verify.scalar(
                select(func.count(Order.id)).where(Order.tenant_id == tenant_id)
            ) == 0
            assert await verify.scalar(
                select(func.count(Customer.id)).where(Customer.tenant_id == tenant_id)
            ) == 0
            assert await verify.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.tenant_id == tenant_id,
                    AuditEvent.event_type.in_(["appointment.created", "order.created"]),
                )
            ) == 0
    finally:
        async with AsyncSessionFactory() as cleanup:
            async with cleanup.begin():
                await cleanup.execute(
                    delete(AuditEvent).where(AuditEvent.tenant_id == tenant_id)
                )
                await cleanup.execute(
                    delete(TelegramOutboundMessage).where(
                        TelegramOutboundMessage.tenant_id == tenant_id
                    )
                )
                await cleanup.execute(
                    delete(TelegramOwnerNotification).where(
                        TelegramOwnerNotification.tenant_id == tenant_id
                    )
                )
                await cleanup.execute(
                    delete(WhatsAppOutboundMessage).where(
                        WhatsAppOutboundMessage.tenant_id == tenant_id
                    )
                )
                await cleanup.execute(
                    delete(BackgroundJob).where(BackgroundJob.tenant_id == tenant_id)
                )
                await cleanup.execute(
                    delete(Appointment).where(Appointment.tenant_id == tenant_id)
                )
                await cleanup.execute(
                    delete(OrderItem).where(OrderItem.tenant_id == tenant_id)
                )
                await cleanup.execute(
                    delete(Order).where(Order.tenant_id == tenant_id)
                )
                await cleanup.execute(
                    delete(BusinessHours).where(BusinessHours.tenant_id == tenant_id)
                )
                await cleanup.execute(
                    delete(Product).where(Product.tenant_id == tenant_id)
                )
                await cleanup.execute(
                    delete(Service).where(Service.tenant_id == tenant_id)
                )
                await cleanup.execute(
                    delete(Customer).where(Customer.tenant_id == tenant_id)
                )
                await cleanup.execute(
                    delete(User).where(User.tenant_id == tenant_id)
                )
                await cleanup.execute(
                    delete(Tenant).where(Tenant.id == tenant_id)
                )
