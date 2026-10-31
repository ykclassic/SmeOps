from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app


def test_owner_can_register_login_and_read_own_identity() -> None:
    slug = f"test-{uuid4().hex[:12]}"
    payload = {
        "business_name": "Test Business",
        "tenant_slug": slug,
        "email": "Owner@Example.com",
        "password": "Correct-Horse-Battery-42",
        "business_timezone": "Africa/Lagos",
    }

    with TestClient(app) as client:
        registration = client.post("/auth/register", json=payload)
        assert registration.status_code == 201
        registration_token = registration.json()["access_token"]

        identity = client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {registration_token}"},
        )
        assert identity.status_code == 200
        assert identity.json()["email"] == "owner@example.com"
        assert identity.json()["role"] == "owner"

        login = client.post(
            "/auth/login",
            json={
                "tenant_slug": slug,
                "email": "owner@example.com",
                "password": payload["password"],
            },
        )
        assert login.status_code == 200
        assert login.json()["token_type"] == "bearer"

        invalid_login = client.post(
            "/auth/login",
            json={
                "tenant_slug": slug,
                "email": "owner@example.com",
                "password": "wrong-password",
            },
        )
        assert invalid_login.status_code == 401


def test_me_rejects_missing_credentials() -> None:
    with TestClient(app) as client:
        response = client.get("/auth/me")
    assert response.status_code == 401
