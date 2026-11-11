from uuid import uuid4

from sqlalchemy import CheckConstraint

from app.db.session import AsyncSessionFactory
from app.models.tenant import Tenant


def test_tenant_status_constraint_includes_deactivated_without_removing_existing_states() -> None:
    constraints = [
        constraint
        for constraint in Tenant.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    ]

    assert len(constraints) == 1
    assert str(constraints[0].sqltext) == (
        "status IN ('active', 'deactivated', 'suspended', 'pending')"
    )


def test_tenant_status_column_can_store_deactivated_value() -> None:
    status_column = Tenant.__table__.c.status

    assert status_column.type.length >= len("deactivated")
    assert status_column.nullable is False


async def test_database_accepts_deactivated_tenant_status() -> None:
    async with AsyncSessionFactory() as session:
        tenant = Tenant(
            name="ADM-007 deactivated status test",
            slug=f"adm007-deactivated-{uuid4().hex}",
            status="deactivated",
        )
        session.add(tenant)
        await session.flush()

        assert tenant.status == "deactivated"
        await session.rollback()
