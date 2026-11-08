from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.main import app


def _register(client: TestClient, slug: str, email: str) -> str:
    response = client.post(
        "/auth/register",
        json={
            "business_name": f"Business {slug}",
            "tenant_slug": slug,
            "email": email,
            "password": "Correct-Horse-Battery-42",
            "business_timezone": "Africa/Lagos",
        },
    )
    assert response.status_code == 201
    return response.json()["access_token"]


def test_tenant_scoped_service_and_appointment_workflow() -> None:
    slug_a = f"appt-{uuid4().hex[:10]}"
    slug_b = f"appt-{uuid4().hex[:10]}"

    with TestClient(app) as client:
        token_a = _register(client, slug_a, f"{slug_a}@example.com")
        token_b = _register(client, slug_b, f"{slug_b}@example.com")
        auth_a = {"Authorization": f"Bearer {token_a}"}
        auth_b = {"Authorization": f"Bearer {token_b}"}

        service_response = client.post(
            "/services",
            headers=auth_a,
            json={
                "name": "Consultation",
                "description": "Initial consultation",
                "duration_minutes": 60,
                "price_minor": 1500000,
            },
        )
        assert service_response.status_code == 201
        service_id = service_response.json()["id"]

        assert client.get("/services", headers=auth_b).json() == []

        hours_response = client.put(
            "/business-hours",
            headers=auth_a,
            json={
                "days": [
                    {
                        "weekday": weekday,
                        "is_closed": False,
                        "opens_at": "08:00:00",
                        "closes_at": "18:00:00",
                    }
                    for weekday in range(7)
                ]
            },
        )
        assert hours_response.status_code == 200
        assert len(hours_response.json()) == 7

        local_now = datetime.now(ZoneInfo("Africa/Lagos"))
        starts_at = (local_now + timedelta(days=2)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        booking_payload = {
            "idempotency_key": str(uuid4()),
            "service_id": service_id,
            "customer_name": "Ada Customer",
            "customer_phone": "+2348012345678",
            "starts_at": starts_at.isoformat(),
        }
        booking = client.post("/appointments", headers=auth_a, json=booking_payload)
        assert booking.status_code == 201
        appointment_id = booking.json()["id"]

        replay = client.post("/appointments", headers=auth_a, json=booking_payload)
        assert replay.status_code == 201
        assert replay.json()["id"] == appointment_id

        changed_request = dict(booking_payload)
        changed_request["starts_at"] = (starts_at + timedelta(hours=2)).isoformat()
        idempotency_conflict = client.post(
            "/appointments", headers=auth_a, json=changed_request
        )
        assert idempotency_conflict.status_code == 409

        overlap_payload = dict(booking_payload)
        overlap_payload["idempotency_key"] = str(uuid4())
        overlap_payload["starts_at"] = (starts_at + timedelta(minutes=30)).isoformat()
        overlap = client.post("/appointments", headers=auth_a, json=overlap_payload)
        assert overlap.status_code == 409

        assert client.get("/appointments", headers=auth_b).json() == []
        cross_tenant_cancel = client.post(
            f"/appointments/{appointment_id}/cancel", headers=auth_b
        )
        assert cross_tenant_cancel.status_code == 404

        cancellation = client.post(
            f"/appointments/{appointment_id}/cancel", headers=auth_a
        )
        assert cancellation.status_code == 200
        assert cancellation.json()["status"] == "cancelled"

        replacement_payload = dict(booking_payload)
        replacement_payload["idempotency_key"] = str(uuid4())
        replacement = client.post(
            "/appointments", headers=auth_a, json=replacement_payload
        )
        assert replacement.status_code == 201


def test_appointment_lifecycle_and_reschedule_validation() -> None:
    slug = f"lifecycle-{uuid4().hex[:10]}"
    with TestClient(app) as client:
        token = _register(client, slug, f"{slug}@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        service = client.post("/services", headers=headers, json={
            "name": "Lifecycle service", "duration_minutes": 60, "price_minor": 2500000,
        })
        assert service.status_code == 201
        service_id = service.json()["id"]
        hours = client.put("/business-hours", headers=headers, json={"days": [
            {"weekday": day, "is_closed": False, "opens_at": "08:00:00", "closes_at": "18:00:00"}
            for day in range(7)
        ]})
        assert hours.status_code == 200
        local_now = datetime.now(ZoneInfo("Africa/Lagos"))
        first_start = (local_now + timedelta(days=3)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        payload = {
            "idempotency_key": str(uuid4()), "service_id": service_id,
            "customer_name": "Lifecycle Customer", "customer_phone": "+2348012345679",
            "starts_at": first_start.isoformat(),
        }
        created = client.post("/appointments", headers=headers, json=payload)
        assert created.status_code == 201
        appointment_id = created.json()["id"]

        moved_start = first_start + timedelta(hours=2)
        moved = client.post(
            f"/appointments/{appointment_id}/reschedule", headers=headers,
            json={"starts_at": moved_start.isoformat()},
        )
        assert moved.status_code == 200
        assert moved.json()["rescheduled_at"] is not None
        assert datetime.fromisoformat(moved.json()["starts_at"]) == moved_start.astimezone(UTC)

        details = client.get(f"/appointments/{appointment_id}", headers=headers)
        assert details.status_code == 200
        completed = client.post(f"/appointments/{appointment_id}/complete", headers=headers)
        assert completed.status_code == 200
        assert completed.json()["status"] == "completed"
        assert completed.json()["completed_at"] is not None
        assert client.post(f"/appointments/{appointment_id}/missed", headers=headers).status_code == 409

        missed_payload = dict(payload)
        missed_payload["idempotency_key"] = str(uuid4())
        missed_payload["customer_phone"] = "+2348012345680"
        missed_payload["starts_at"] = (first_start + timedelta(days=1)).isoformat()
        missed_booking = client.post("/appointments", headers=headers, json=missed_payload)
        assert missed_booking.status_code == 201
        missed = client.post(
            f"/appointments/{missed_booking.json()['id']}/missed", headers=headers,
        )
        assert missed.status_code == 200
        assert missed.json()["status"] == "missed"

        other_slug = f"other-{uuid4().hex[:10]}"
        other_token = _register(client, other_slug, f"{other_slug}@example.com")
        cross_tenant = client.get(
            f"/appointments/{appointment_id}",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert cross_tenant.status_code == 404
