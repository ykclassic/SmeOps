import asyncio
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update

from app.core.security import create_access_token
from app.db.session import AsyncSessionFactory
from app.main import app
from app.models import AuditEvent, Tenant, User

PASSWORD = "Correct-Horse-Battery-42"


def _register_owner(client: TestClient) -> tuple[str, str, str, str]:
    slug = f"adm010-{uuid4().hex[:16]}"
    response = client.post(
        "/auth/register",
        json={
            "business_name": "ADM-010 API test",
            "tenant_slug": slug,
            "email": f"{slug}@example.com",
            "password": PASSWORD,
            "business_timezone": "Africa/Lagos",
        },
    )
    assert response.status_code == 201
    token = response.json()["access_token"]
    identity = client.get("/auth/me", headers=_headers(token))
    assert identity.status_code == 200
    data = identity.json()
    return token, slug, data["tenant_id"], data["id"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _set_tenant_status(tenant_id: str, value: str) -> None:
    async def update_status() -> None:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                await session.execute(
                    update(Tenant).where(Tenant.id == UUID(tenant_id)).values(status=value)
                )
    asyncio.run(update_status())


def _set_role(tenant_id: str, user_id: str, role: str) -> str:
    async def update_role() -> None:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                user = await session.get(User, UUID(user_id))
                assert user is not None and str(user.tenant_id) == tenant_id
                user.role = role
    asyncio.run(update_role())
    return create_access_token(user_id=UUID(user_id), tenant_id=UUID(tenant_id))


def _evidence(tenant_id: str) -> tuple[str, int]:
    async def read() -> tuple[str, int]:
        async with AsyncSessionFactory() as session:
            tenant_status = await session.scalar(
                select(Tenant.status).where(Tenant.id == UUID(tenant_id))
            )
            count = await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.tenant_id == UUID(tenant_id),
                    AuditEvent.event_type == "tenant.reactivated",
                )
            )
            return tenant_status, count or 0
    return asyncio.run(read())


def _cleanup(tenant_id: str) -> None:
    async def cleanup() -> None:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                await session.execute(
                    delete(AuditEvent).where(AuditEvent.tenant_id == UUID(tenant_id))
                )
                await session.execute(delete(User).where(User.tenant_id == UUID(tenant_id)))
                await session.execute(delete(Tenant).where(Tenant.id == UUID(tenant_id)))
    asyncio.run(cleanup())


def _reactivate(client: TestClient, token: str, **overrides):
    payload = {"confirmation": "REACTIVATE", "password": PASSWORD}
    payload.update(overrides)
    return client.post("/account/reactivate", headers=_headers(token), json=payload)


def test_deactivated_owner_can_login_but_only_use_lifecycle_endpoints():
    with TestClient(app) as client:
        _, slug, tenant_id, _ = _register_owner(client)
        _set_tenant_status(tenant_id, "deactivated")

        login = client.post(
            "/auth/login",
            json={"tenant_slug": slug, "email": f"{slug}@example.com", "password": PASSWORD},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]

        assert client.get("/auth/me", headers=_headers(token)).status_code == 401
        assert client.get("/services", headers=_headers(token)).status_code == 401
        lifecycle = client.get("/account/lifecycle", headers=_headers(token))
        assert lifecycle.status_code == 200
        assert lifecycle.json()["status"] == "deactivated"
    try:
        assert _evidence(tenant_id) == ("deactivated", 0)
    finally:
        _cleanup(tenant_id)


def test_reactivation_requires_exact_confirmation():
    with TestClient(app) as client:
        token, _, tenant_id, _ = _register_owner(client)
        _set_tenant_status(tenant_id, "deactivated")
        response = _reactivate(client, token, confirmation="ACTIVATE")
    try:
        assert response.status_code == 422
        assert _evidence(tenant_id) == ("deactivated", 0)
    finally:
        _cleanup(tenant_id)


def test_reactivation_requires_current_password():
    with TestClient(app) as client:
        token, _, tenant_id, _ = _register_owner(client)
        _set_tenant_status(tenant_id, "deactivated")
        response = _reactivate(client, token, password="Incorrect-Password-73")
    try:
        assert response.status_code == 401
        assert _evidence(tenant_id) == ("deactivated", 0)
    finally:
        _cleanup(tenant_id)


def test_reactivation_reads_fresh_password_hash_after_concurrent_change():
    from app.api.dependencies import DbSession, get_lifecycle_owner
    from app.core.security import hash_password

    new_password = "New-Password-After-Change-73"
    with TestClient(app) as client:
        token, _, tenant_id, user_id = _register_owner(client)
        _set_tenant_status(tenant_id, "deactivated")

        async def stale_owner(session: DbSession) -> User:
            owner = await session.get(User, UUID(user_id))
            assert owner is not None
            async with AsyncSessionFactory() as other:
                async with other.begin():
                    current = await other.get(User, UUID(user_id))
                    assert current is not None
                    current.password_hash = hash_password(new_password)
            return owner

        app.dependency_overrides[get_lifecycle_owner] = stale_owner
        try:
            old_password = _reactivate(client, token)
            new_password_response = _reactivate(client, token, password=new_password)
        finally:
            app.dependency_overrides.pop(get_lifecycle_owner, None)
    try:
        assert old_password.status_code == 401
        assert new_password_response.status_code == 200
        assert _evidence(tenant_id) == ("active", 1)
    finally:
        _cleanup(tenant_id)


def test_reactivation_commits_tenant_and_audit_atomically():
    with TestClient(app) as client:
        token, _, tenant_id, _ = _register_owner(client)
        _set_tenant_status(tenant_id, "deactivated")
        response = _reactivate(client, token, reason="Owner confirmed reactivation")
        assert response.status_code == 200
        assert response.json() == {
            "status": "active",
            "changed": True,
            "tenant_id": tenant_id,
        }
        assert client.get("/auth/me", headers=_headers(token)).status_code == 200
        assert client.get("/services", headers=_headers(token)).status_code == 200
    try:
        assert _evidence(tenant_id) == ("active", 1)
    finally:
        _cleanup(tenant_id)


def test_reactivation_rate_limit_persists_across_requests_and_sessions():
    with TestClient(app) as client:
        token, _, tenant_id, _ = _register_owner(client)
        _set_tenant_status(tenant_id, "deactivated")
        responses = [
            _reactivate(client, token, password="Wrong-Password-73")
            for _ in range(6)
        ]
    try:
        assert [response.status_code for response in responses[:5]] == [401] * 5
        assert responses[5].status_code == 429
        assert "Retry-After" in responses[5].headers
        assert _evidence(tenant_id) == ("deactivated", 0)
    finally:
        _cleanup(tenant_id)


@pytest.mark.parametrize("tenant_status", ["suspended", "pending"])
def test_nonreactivatable_tenant_cannot_use_lifecycle_auth(tenant_status: str):
    with TestClient(app) as client:
        token, slug, tenant_id, _ = _register_owner(client)
        _set_tenant_status(tenant_id, tenant_status)
        login = client.post(
            "/auth/login",
            json={"tenant_slug": slug, "email": f"{slug}@example.com", "password": PASSWORD},
        )
        status_response = client.get("/account/lifecycle", headers=_headers(token))
        reactivate_response = _reactivate(client, token)
    try:
        assert login.status_code == 401
        assert status_response.status_code == 401
        assert reactivate_response.status_code == 401
        assert _evidence(tenant_id) == (tenant_status, 0)
    finally:
        _cleanup(tenant_id)


def test_manager_cannot_use_lifecycle_reactivation():
    with TestClient(app) as client:
        token, _, tenant_id, user_id = _register_owner(client)
        manager_token = _set_role(tenant_id, user_id, "manager")
        _set_tenant_status(tenant_id, "deactivated")
        response = _reactivate(client, manager_token)
    try:
        assert response.status_code == 401
        assert _evidence(tenant_id) == ("deactivated", 0)
    finally:
        _cleanup(tenant_id)


def test_owner_reactivation_does_not_enable_deactivation_endpoint():
    with TestClient(app) as client:
        token, _, tenant_id, _ = _register_owner(client)
        _set_tenant_status(tenant_id, "deactivated")
        response = _reactivate(client, token)
        # Once active, the deactivation API must still be hidden by its
        # independent, disabled-by-default activation safeguard.
        deactivate = client.post(
            "/account/deactivate",
            headers=_headers(token),
            json={"confirmation": "DEACTIVATE", "password": PASSWORD},
        )
    try:
        assert response.status_code == 200
        assert deactivate.status_code == 404
        assert _evidence(tenant_id) == ("active", 1)
    finally:
        _cleanup(tenant_id)
