"""Verify tenant status migration preservation and downgrade safety.

CI runs this against an isolated PostgreSQL database after migrating to 0023.
"""
import asyncio
import sys
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings

TENANTS = (
    (UUID("00000000-0000-0000-0000-000000000701"), "adm007-active", "active"),
    (UUID("00000000-0000-0000-0000-000000000702"), "adm007-suspended", "suspended"),
    (UUID("00000000-0000-0000-0000-000000000703"), "adm007-pending", "pending"),
)
DEACTIVATED_TENANT = (
    UUID("00000000-0000-0000-0000-000000000704"),
    "adm007-deactivated",
)


async def seed_existing_statuses(connection) -> None:
    await connection.execute(
        text(
            "INSERT INTO tenants (id, name, slug, business_timezone, status) "
            "VALUES (:id, :name, :slug, 'Africa/Lagos', :status)"
        ),
        [
            {"id": tenant_id, "name": slug, "slug": slug, "status": status}
            for tenant_id, slug, status in TENANTS
        ],
    )


async def verify_existing_statuses(connection) -> None:
    statement = text(
        "SELECT id, status FROM tenants WHERE id IN :tenant_ids"
    ).bindparams(bindparam("tenant_ids", expanding=True))
    result = await connection.execute(
        statement,
        {"tenant_ids": [tenant_id for tenant_id, _, _ in TENANTS]},
    )
    actual = {row.id: row.status for row in result}
    expected = {tenant_id: status for tenant_id, _, status in TENANTS}
    if actual != expected:
        raise AssertionError(f"Tenant statuses changed during migration: {actual!r}")
    print("Existing active/suspended/pending tenant rows survived migration.")


async def seed_deactivated(connection) -> None:
    tenant_id, slug = DEACTIVATED_TENANT
    await connection.execute(
        text(
            "INSERT INTO tenants (id, name, slug, business_timezone, status) "
            "VALUES (:id, :name, :slug, 'Africa/Lagos', 'deactivated')"
        ),
        {"id": tenant_id, "name": slug, "slug": slug},
    )


async def verify_deactivated(connection) -> None:
    tenant_id, _ = DEACTIVATED_TENANT
    status = await connection.scalar(
        text("SELECT status FROM tenants WHERE id = :tenant_id"),
        {"tenant_id": tenant_id},
    )
    if status != "deactivated":
        raise AssertionError(
            f"Downgrade did not preserve deactivated tenant state: {status!r}"
        )
    print("Downgrade guard preserved the deactivated tenant row.")


async def reactivate_deactivated(connection) -> None:
    tenant_id, _ = DEACTIVATED_TENANT
    await connection.execute(
        text("UPDATE tenants SET status = 'active' WHERE id = :tenant_id"),
        {"tenant_id": tenant_id},
    )


async def main(action: str) -> None:
    actions = {
        "seed": seed_existing_statuses,
        "verify": verify_existing_statuses,
        "seed-deactivated": seed_deactivated,
        "verify-deactivated": verify_deactivated,
        "reactivate-deactivated": reactivate_deactivated,
    }
    if action not in actions:
        raise SystemExit(f"Unknown action: {action}")

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            await actions[action](connection)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(
            "Usage: verify_tenant_status_migration.py "
            "seed|verify|seed-deactivated|verify-deactivated|reactivate-deactivated"
        )
    asyncio.run(main(sys.argv[1]))
