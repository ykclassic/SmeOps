import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session

from app.api.dependencies import DbSession, get_current_user
from app.core.security import create_access_token, hash_password
from app.db.session import AsyncSessionFactory
from app.main import app
from app.models import AuditEvent, Tenant, User

PASSWORD = "Correct-Horse-Battery-42"


def _register_owner(client: TestClient) -> tuple[str, str, str]:
    slug = f"adm009-{uuid4().hex[:16]}"
    response = client.post(
        "/auth/register",
        json={
            "business_name": "ADM-009 API test",
            "tenant_slug": slug,
            "email": f"{slug}@example.com",
            "password": PASSWORD,
            "business_timezone": "Africa/Lagos",
        },
    )
    assert response.status_code == 201
    token = response.json()["access_token"]
    identity = client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert identity.status_code == 200
    data = identity.json()
    return token, data["tenant_id"], data["id"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _enable_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.api.account.get_settings",
        lambda: SimpleNamespace(owner_deactivation_api_enabled=True),
    )


def _set_role_and_issue_token(tenant_id: str, user_id: str, role: str) -> str:
    async def update_role() -> None:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                user = await session.get(User, UUID(user_id))
                assert user is not None
                assert str(user.tenant_id) == tenant_id
                user.role = role

    asyncio.run(update_role())
    return create_access_token(
        user_id=UUID(user_id),
        tenant_id=UUID(tenant_id),
    )


def _lifecycle_evidence(tenant_id: str) -> tuple[str, int]:
    async def read_evidence() -> tuple[str, int]:
        async with AsyncSessionFactory() as session:
            tenant_status = await session.scalar(
                select(Tenant.status).where(Tenant.id == UUID(tenant_id))
            )
            count = await session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.tenant_id == UUID(tenant_id),
                    AuditEvent.event_type == "tenant.deactivated",
                )
            )
            return tenant_status, count or 0

    return asyncio.run(read_evidence())


def _cleanup_tenant(tenant_id: str) -> None:
    async def cleanup() -> None:
        async with AsyncSessionFactory() as session:
            async with session.begin():
                await session.execute(
                    delete(AuditEvent).where(AuditEvent.tenant_id == UUID(tenant_id))
                )
                await session.execute(
                    delete(User).where(User.tenant_id == UUID(tenant_id))
                )
                await session.execute(delete(Tenant).where(Tenant.id == UUID(tenant_id)))

    asyncio.run(cleanup())


def test_owner_deactivation_requires_authentication() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/account/deactivate",
            json={"confirmation": "DEACTIVATE", "password": PASSWORD},
        )
    assert response.status_code == 401


def test_owner_deactivation_endpoint_is_disabled_by_default() -> None:
    with TestClient(app) as client:
        token, tenant_id, _ = _register_owner(client)
        response = client.post(
            "/account/deactivate",
            headers=_headers(token),
            json={"confirmation": "DEACTIVATE", "password": PASSWORD},
        )

    try:
        assert response.status_code == 404
        assert _lifecycle_evidence(tenant_id) == ("active", 0)
    finally:
        _cleanup_tenant(tenant_id)


def test_owner_deactivation_requires_exact_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_endpoint(monkeypatch)
    with TestClient(app) as client:
        token, tenant_id, _ = _register_owner(client)
        response = client.post(
            "/account/deactivate",
            headers=_headers(token),
            json={"confirmation": "DELETE", "password": PASSWORD},
        )

    try:
        assert response.status_code == 422
        assert _lifecycle_evidence(tenant_id) == ("active", 0)
    finally:
        _cleanup_tenant(tenant_id)


def test_owner_deactivation_requires_password_reauthentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_endpoint(monkeypatch)
    with TestClient(app) as client:
        token, tenant_id, _ = _register_owner(client)
        response = client.post(
            "/account/deactivate",
            headers=_headers(token),
            json={"confirmation": "DEACTIVATE", "password": "Wrong-Password-42"},
        )

    try:
        assert response.status_code == 401
        assert _lifecycle_evidence(tenant_id) == ("active", 0)
    finally:
        _cleanup_tenant(tenant_id)


@pytest.mark.parametrize("role", ["manager", "staff"])
def test_non_owner_cannot_deactivate_tenant(
    role: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_endpoint(monkeypatch)
    with TestClient(app) as client:
        _, tenant_id, owner_id = _register_owner(client)
        token = _set_role_and_issue_token(tenant_id, owner_id, role)
        response = client.post(
            "/account/deactivate",
            headers=_headers(token),
            json={"confirmation": "DEACTIVATE", "password": PASSWORD},
        )

    try:
        assert response.status_code == 403
        assert _lifecycle_evidence(tenant_id) == ("active", 0)
    finally:
        _cleanup_tenant(tenant_id)


def test_request_cannot_select_another_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_endpoint(monkeypatch)
    with TestClient(app) as client:
        token_a, tenant_a, _ = _register_owner(client)
        _, tenant_b, _ = _register_owner(client)
        response = client.post(
            "/account/deactivate",
            headers=_headers(token_a),
            json={
                "confirmation": "DEACTIVATE",
                "password": PASSWORD,
                "tenant_id": tenant_b,
            },
        )

    try:
        assert response.status_code == 422
        assert _lifecycle_evidence(tenant_a) == ("active", 0)
        assert _lifecycle_evidence(tenant_b) == ("active", 0)
    finally:
        _cleanup_tenant(tenant_a)
        _cleanup_tenant(tenant_b)


def test_owner_deactivation_commits_state_and_audit_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_endpoint(monkeypatch)
    with TestClient(app) as client:
        token, tenant_id, _ = _register_owner(client)
        response = client.post(
            "/account/deactivate",
            headers=_headers(token),
            json={
                "confirmation": "DEACTIVATE",
                "password": PASSWORD,
                "reason": "Owner requested account deactivation",
            },
        )

    try:
        assert response.status_code == 200
        assert response.json() == {
            "status": "deactivated",
            "changed": True,
            "tenant_id": tenant_id,
        }
        assert _lifecycle_evidence(tenant_id) == ("deactivated", 1)
    finally:
        _cleanup_tenant(tenant_id)


def test_audit_insert_failure_rolls_back_api_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_endpoint(monkeypatch)
    with TestClient(app, raise_server_exceptions=False) as client:
        token, tenant_id, _ = _register_owner(client)

        def fail_audit_flush(sync_session, flush_context, instances) -> None:
            if any(isinstance(row, AuditEvent) for row in sync_session.new):
                raise RuntimeError("simulated audit insert failure")

        event.listen(Session, "before_flush", fail_audit_flush)
        try:
            response = client.post(
                "/account/deactivate",
                headers=_headers(token),
                json={"confirmation": "DEACTIVATE", "password": PASSWORD},
            )
        finally:
            event.remove(Session, "before_flush", fail_audit_flush)

    try:
        assert response.status_code == 500
        assert _lifecycle_evidence(tenant_id) == ("active", 0)
    finally:
        _cleanup_tenant(tenant_id)


def test_owner_deactivation_uses_fresh_password_hash_after_concurrent_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_endpoint(monkeypatch)
    new_password = "New-Password-After-Change-73"

    with TestClient(app) as client:
        token, tenant_id, user_id = _register_owner(client)

        async def stale_authenticated_user(session: DbSession) -> User:
            # Resolve authentication first, retaining the original ORM instance
            # in the request session's identity map.
            stale_user = await session.get(User, UUID(user_id))
            assert stale_user is not None

            # Simulate a password change committed by another request/session
            # after authentication resolution but before step-up verification.
            async with AsyncSessionFactory() as password_change_session:
                async with password_change_session.begin():
                    current_user = await password_change_session.get(
                        User, UUID(user_id)
                    )
                    assert current_user is not None
                    current_user.password_hash = hash_password(new_password)

            return stale_user

        app.dependency_overrides[get_current_user] = stale_authenticated_user
        try:
            stale_password_response = client.post(
                "/account/deactivate",
                headers=_headers(token),
                json={
                    "confirmation": "DEACTIVATE",
                    "password": PASSWORD,
                },
            )
            fresh_password_response = client.post(
                "/account/deactivate",
                headers=_headers(token),
                json={
                    "confirmation": "DEACTIVATE",
                    "password": new_password,
                },
            )
        finally:
            app.dependency_overrides.pop(get_current_user, None)

    try:
        assert stale_password_response.status_code == 401
        assert fresh_password_response.status_code == 200
        assert _lifecycle_evidence(tenant_id) == ("deactivated", 1)
    finally:
        _cleanup_tenant(tenant_id)
