import asyncio
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, event, func, select

from app.db.session import AsyncSessionFactory
from app.models import AuditEvent, BusinessHours, Service, Tenant, User
from app.services.tenant_lifecycle import (
    LifecycleTransitionError,
    transition_tenant_lifecycle,
)


async def _create_tenant(*, status: str = "active", owner_role: str = "owner"):
    tenant_id = uuid4()
    owner_id = uuid4()
    manager_id = uuid4()
    async with AsyncSessionFactory() as session:
        async with session.begin():
            session.add(
                Tenant(
                    id=tenant_id,
                    name="ADM-008 lifecycle test",
                    slug=f"adm008-{uuid4().hex}",
                    status=status,
                )
            )
            session.add_all(
                [
                    User(
                        id=owner_id,
                        tenant_id=tenant_id,
                        email=f"{owner_id.hex}@example.test",
                        password_hash="test-only-hash",
                        role=owner_role,
                        status="active",
                    ),
                    User(
                        id=manager_id,
                        tenant_id=tenant_id,
                        email=f"{manager_id.hex}@example.test",
                        password_hash="test-only-hash",
                        role="manager",
                        status="active",
                    ),
                ]
            )
    return tenant_id, owner_id, manager_id


async def _cleanup_tenant(tenant_id):
    async with AsyncSessionFactory() as session:
        async with session.begin():
            await session.execute(delete(AuditEvent).where(AuditEvent.tenant_id == tenant_id))
            await session.execute(delete(User).where(User.tenant_id == tenant_id))
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


async def _status_and_audit_count(tenant_id):
    async with AsyncSessionFactory() as session:
        tenant_status = await session.scalar(
            select(Tenant.status).where(Tenant.id == tenant_id)
        )
        event_count = await session.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.event_type.in_(
                    ["tenant.deactivated", "tenant.reactivated"]
                ),
            )
        )
    return tenant_status, event_count


@pytest.mark.parametrize(
    ("initial_status", "target_status", "expected_status"),
    [
        ("active", "deactivated", "deactivated"),
        ("deactivated", "active", "active"),
    ],
)
async def test_owner_transition_updates_state_and_audit_atomically(
    initial_status, target_status, expected_status
):
    tenant_id, owner_id, _ = await _create_tenant(status=initial_status)
    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                result = await transition_tenant_lifecycle(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=owner_id,
                    target_status=target_status,
                    reason="Owner requested account lifecycle change",
                )

        assert result.changed is True
        assert result.status == expected_status
        assert result.audit_event_id is not None
        actual_status, event_count = await _status_and_audit_count(tenant_id)
        assert actual_status == expected_status
        assert event_count == 1
    finally:
        await _cleanup_tenant(tenant_id)


async def test_repeated_transition_is_noop_without_duplicate_audit():
    tenant_id, owner_id, _ = await _create_tenant()
    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                first = await transition_tenant_lifecycle(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=owner_id,
                    target_status="deactivated",
                )
        async with AsyncSessionFactory() as session:
            async with session.begin():
                second = await transition_tenant_lifecycle(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=owner_id,
                    target_status="deactivated",
                )

        assert first.changed is True
        assert second.changed is False
        assert second.audit_event_id is None
        actual_status, event_count = await _status_and_audit_count(tenant_id)
        assert actual_status == "deactivated"
        assert event_count == 1
    finally:
        await _cleanup_tenant(tenant_id)


async def test_service_does_not_commit_unrelated_caller_work():
    tenant_id, owner_id, _ = await _create_tenant()
    unrelated_tenant_id = uuid4()
    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                session.add(
                    Tenant(
                        id=unrelated_tenant_id,
                        name="Unrelated uncommitted tenant",
                        slug=f"adm008-unrelated-{uuid4().hex}",
                        status="active",
                    )
                )
                await transition_tenant_lifecycle(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=owner_id,
                    target_status="deactivated",
                )

                # A separate connection cannot see the caller's unrelated
                # pending row until the caller commits the outer transaction.
                async with AsyncSessionFactory() as observer:
                    assert await observer.get(Tenant, unrelated_tenant_id) is None

        async with AsyncSessionFactory() as observer:
            assert await observer.get(Tenant, unrelated_tenant_id) is not None
    finally:
        await _cleanup_tenant(tenant_id)
        async with AsyncSessionFactory() as session:
            async with session.begin():
                await session.execute(
                    delete(Tenant).where(Tenant.id == unrelated_tenant_id)
                )


async def test_audit_flush_failure_rolls_back_transition_savepoint():
    tenant_id, owner_id, _ = await _create_tenant()
    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                def fail_audit_flush(sync_session, flush_context, instances):
                    if any(isinstance(row, AuditEvent) for row in sync_session.new):
                        raise RuntimeError("simulated audit insert failure")

                event.listen(session.sync_session, "before_flush", fail_audit_flush)
                try:
                    with pytest.raises(RuntimeError, match="simulated audit insert failure"):
                        await transition_tenant_lifecycle(
                            session,
                            tenant_id=tenant_id,
                            actor_user_id=owner_id,
                            target_status="deactivated",
                        )
                finally:
                    event.remove(session.sync_session, "before_flush", fail_audit_flush)

                # The SAVEPOINT rolled back only the service's changes. The
                # caller's outer transaction remains usable.
                assert session.in_transaction()
                tenant_status = await session.scalar(
                    select(Tenant.status)
                    .where(Tenant.id == tenant_id)
                    .execution_options(populate_existing=True)
                )
                event_count = await session.scalar(
                    select(func.count(AuditEvent.id)).where(
                        AuditEvent.tenant_id == tenant_id,
                        AuditEvent.event_type == "tenant.deactivated",
                    )
                )
                assert tenant_status == "active"
                assert event_count == 0

        actual_status, event_count = await _status_and_audit_count(tenant_id)
        assert actual_status == "active"
        assert event_count == 0
    finally:
        await _cleanup_tenant(tenant_id)


async def test_suspended_tenant_owner_cannot_self_reactivate():
    tenant_id, owner_id, _ = await _create_tenant(status="suspended")
    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                with pytest.raises(LifecycleTransitionError, match="not permitted"):
                    await transition_tenant_lifecycle(
                        session,
                        tenant_id=tenant_id,
                        actor_user_id=owner_id,
                        target_status="active",
                    )

        actual_status, event_count = await _status_and_audit_count(tenant_id)
        assert actual_status == "suspended"
        assert event_count == 0
    finally:
        await _cleanup_tenant(tenant_id)


async def test_manager_cannot_execute_owner_lifecycle_transition():
    tenant_id, _, manager_id = await _create_tenant()
    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                with pytest.raises(LifecycleTransitionError, match="active owner"):
                    await transition_tenant_lifecycle(
                        session,
                        tenant_id=tenant_id,
                        actor_user_id=manager_id,
                        target_status="deactivated",
                    )

        actual_status, event_count = await _status_and_audit_count(tenant_id)
        assert actual_status == "active"
        assert event_count == 0
    finally:
        await _cleanup_tenant(tenant_id)


async def test_duplicate_concurrent_deactivation_is_idempotent():
    tenant_id, owner_id, _ = await _create_tenant()
    try:
        async def deactivate():
            async with AsyncSessionFactory() as session:
                async with session.begin():
                    return await transition_tenant_lifecycle(
                        session,
                        tenant_id=tenant_id,
                        actor_user_id=owner_id,
                        target_status="deactivated",
                        reason="Owner request",
                    )

        first, second = await asyncio.gather(deactivate(), deactivate())

        assert sorted([first.changed, second.changed]) == [False, True]
        assert first.status == second.status == "deactivated"
        actual_status, event_count = await _status_and_audit_count(tenant_id)
        assert actual_status == "deactivated"
        assert event_count == 1
    finally:
        await _cleanup_tenant(tenant_id)


async def test_deactivation_serializes_against_real_salon_booking_path(monkeypatch):
    tenant_id, owner_id, _ = await _create_tenant()
    timezone = ZoneInfo("Africa/Lagos")
    local_start = (datetime.now(timezone) + timedelta(days=2)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    service_id = uuid4()
    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                session.add(
                    Service(
                        id=service_id,
                        tenant_id=tenant_id,
                        name="ADM-008 test service",
                        duration_minutes=30,
                        price_minor=1000,
                        currency="NGN",
                        is_active=True,
                    )
                )
                session.add(
                    BusinessHours(
                        tenant_id=tenant_id,
                        weekday=local_start.weekday(),
                        is_closed=False,
                        opens_at=time(9, 0),
                        closes_at=time(17, 0),
                    )
                )

        async def no_op_owner_alert(*args, **kwargs):
            return None

        monkeypatch.setattr(
            "app.services.salon_booking.enqueue_appointment_owner_alerts",
            no_op_owner_alert,
        )
        booking_started = asyncio.Event()

        async def attempt_booking():
            from app.services.salon_booking import (
                BookingConflict,
                create_appointment_booking,
            )

            async with AsyncSessionFactory() as session:
                async with session.begin():
                    booking_started.set()
                    await create_appointment_booking(
                        session,
                        tenant_id=tenant_id,
                        service_id=service_id,
                        customer_name="Lifecycle concurrency test",
                        customer_phone="+2348012345678",
                        starts_at=local_start.astimezone(UTC),
                        conversation_id=uuid4(),
                        booking_attempt_id=uuid4(),
                        source="test",
                    )

        async with AsyncSessionFactory() as lifecycle_session:
            async with lifecycle_session.begin():
                await transition_tenant_lifecycle(
                    lifecycle_session,
                    tenant_id=tenant_id,
                    actor_user_id=owner_id,
                    target_status="deactivated",
                )

                booking_task = asyncio.create_task(attempt_booking())
                await asyncio.wait_for(booking_started.wait(), timeout=2)
                await asyncio.sleep(0.15)
                assert not booking_task.done(), (
                    "Booking should wait on the tenant row lock held by lifecycle transaction"
                )

            from app.services.salon_booking import BookingConflict

            with pytest.raises(BookingConflict, match="Business is unavailable"):
                await booking_task

        actual_status, event_count = await _status_and_audit_count(tenant_id)
        assert actual_status == "deactivated"
        assert event_count == 1
    finally:
        await _cleanup_tenant(tenant_id)


@pytest.mark.parametrize(
    "reason",
    [
        "x" * 501,
        "contains\nnewline",
        "contains\x7fdelete",
    ],
)
async def test_reason_rejects_oversized_or_control_characters(reason):
    tenant_id, owner_id, _ = await _create_tenant()
    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                with pytest.raises(LifecycleTransitionError, match="Reason"):
                    await transition_tenant_lifecycle(
                        session,
                        tenant_id=tenant_id,
                        actor_user_id=owner_id,
                        target_status="deactivated",
                        reason=reason,
                    )

        actual_status, event_count = await _status_and_audit_count(tenant_id)
        assert actual_status == "active"
        assert event_count == 0
    finally:
        await _cleanup_tenant(tenant_id)


async def test_cross_tenant_actor_cannot_transition_another_tenant():
    tenant_id, _, _ = await _create_tenant()
    other_tenant_id, _, other_owner_id = await _create_tenant()
    try:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                with pytest.raises(LifecycleTransitionError, match="active owner"):
                    await transition_tenant_lifecycle(
                        session,
                        tenant_id=tenant_id,
                        actor_user_id=other_owner_id,
                        target_status="deactivated",
                    )

        actual_status, event_count = await _status_and_audit_count(tenant_id)
        assert actual_status == "active"
        assert event_count == 0
    finally:
        await _cleanup_tenant(tenant_id)
        await _cleanup_tenant(other_tenant_id)
