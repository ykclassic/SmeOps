from sqlalchemy import CheckConstraint

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
