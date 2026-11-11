import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from app.db.session import AsyncSessionFactory
from app.models import AuditEvent, Tenant, User
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
    ("initial_status", "target_status", "expected_status", "expected_changed"),
    [
        ("active", "deactivated", "deactivated", True),
        ("deactivated", "active", "active", True),
    ],
)
async def test_owner_transition_updates_state_and_audit_atomically(
    initial_status, target_status, expected_status, expected_changed
):
    tenant_id, owner_id, _ = await _create_tenant(status=initial_status)
    try:
        async with AsyncSessionFactory() as session:
            result = await transition_tenant_lifecycle(
                session,
                tenant_id=tenant_id,
                actor_user_id=owner_id,
                target_status=target_status,
                reason="Owner requested account lifecycle change",
            )

        assert result.changed is expected_changed
        assert result.status == expected_status
        assert result.audit_event_id is not None
        actual_status, event_count = await _status_and_audit_count(tenant_id)
        assert actual_status == expected_status
        assert event_count == 1
    finally:
        await _cleanup_tenant(tenant_id)


async def test_suspended_tenant_owner_cannot_self_reactivate():
    tenant_id, owner_id, _ = await _create_tenant(status="suspended")
    try:
        async with AsyncSessionFactory() as session:
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
    other_tenant_id, other_owner_id, _ = await _create_tenant()
    try:
        async with AsyncSessionFactory() as session:
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
