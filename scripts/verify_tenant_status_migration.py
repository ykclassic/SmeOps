"""Exercise the tenant-status migration against pre-existing tenant rows.

Usage:
    python scripts/verify_tenant_status_migration.py seed
    python scripts/verify_tenant_status_migration.py verify

The CI workflow downgrades only the new 0017 revision before seeding, then
upgrades it again and verifies that all pre-existing statuses survive.
"""
import asyncio
import sys
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings

TENANTS = (
    (UUID("00000000-0000-0000-0000-000000000701"), "adm007-active", "active"),
    (UUID("00000000-0000-0000-0000-000000000702"), "adm007-suspended", "suspended"),
    (UUID("00000000-0000-0000-0000-000000000703"), "adm007-pending", "pending"),
)


async def seed(connection) -> None:
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


async def verify(connection) -> None:
    result = await connection.execute(
        text("SELECT id, status FROM tenants WHERE id = ANY(:tenant_ids)"),
        {"tenant_ids": [tenant_id for tenant_id, _, _ in TENANTS]},
    )
    actual = {row.id: row.status for row in result}
    expected = {tenant_id: status for tenant_id, _, status in TENANTS}
    if actual != expected:
        raise AssertionError(f"Tenant statuses changed during migration: {actual!r}")
    print("ADM-007 populated-database migration preserved active/suspended/pending rows.")


async def main(action: str) -> None:
    if action not in {"seed", "verify"}:
        raise SystemExit("Expected action: seed or verify")

    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            if action == "seed":
                await seed(connection)
                print("Seeded existing tenant statuses at migration revision 0016.")
            else:
                await verify(connection)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: verify_tenant_status_migration.py seed|verify")
    asyncio.run(main(sys.argv[1]))
