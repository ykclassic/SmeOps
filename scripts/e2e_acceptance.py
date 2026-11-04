import os
import sys
from typing import Any

import httpx


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Required E2E environment variable is missing: {name}")
    return value


def normalize_phone(value: str) -> str:
    return "".join(character for character in value if character.isdigit())


def request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    try:
        response = client.request(method, path, headers=headers, json=payload)
        response.raise_for_status()
        return response.json() if response.content else None
    except (httpx.HTTPError, ValueError) as exc:
        raise SystemExit(
            f"E2E request failed for {method} {path}; inspect sanitized deployment logs."
        ) from exc


def main() -> int:
    base_url = required("E2E_BASE_URL").rstrip("/")
    tenant_slug = required("E2E_TEST_TENANT_SLUG")
    email = required("E2E_TEST_EMAIL")
    password = required("E2E_TEST_PASSWORD")
    verify_booking = os.getenv("E2E_VERIFY_CUSTOMER_BOOKING", "false").casefold() == "true"

    if not base_url.startswith("https://") and "localhost" not in base_url:
        raise SystemExit("E2E_BASE_URL must use HTTPS outside local development.")

    with httpx.Client(base_url=base_url, timeout=20.0, follow_redirects=False) as client:
        request_json(client, "GET", "/health/live")
        request_json(client, "GET", "/health/ready")
        request_json(client, "GET", "/health/worker")

        token_payload = request_json(
            client,
            "POST",
            "/auth/login",
            payload={
                "tenant_slug": tenant_slug,
                "email": email,
                "password": password,
            },
        )
        access_token = (
            token_payload.get("access_token")
            if isinstance(token_payload, dict)
            else None
        )
        if not isinstance(access_token, str) or not access_token:
            raise SystemExit("Test account login did not return an access token.")

        auth_headers = {"Authorization": f"Bearer {access_token}"}
        identity = request_json(client, "GET", "/auth/me", headers=auth_headers)
        if not isinstance(identity, dict) or not identity.get("tenant_id"):
            raise SystemExit("Authenticated identity response is incomplete.")

        services = request_json(client, "GET", "/services", headers=auth_headers)
        hours = request_json(client, "GET", "/business-hours", headers=auth_headers)
        connection = request_json(
            client,
            "GET",
            "/whatsapp/connection",
            headers=auth_headers,
        )
        appointments = request_json(
            client,
            "GET",
            "/appointments?limit=100",
            headers=auth_headers,
        )
        conversations = request_json(
            client,
            "GET",
            "/whatsapp/conversations?limit=100",
            headers=auth_headers,
        )

        if not isinstance(services, list) or not services:
            raise SystemExit("Test tenant must have at least one active salon service.")
        if not isinstance(hours, list) or len(hours) != 7:
            raise SystemExit("Test tenant must have all seven business-hours records.")
        if not isinstance(connection, dict) or connection.get("status") != "active":
            raise SystemExit("Test tenant must have an active WhatsApp Business connection.")
        if not isinstance(appointments, list) or not isinstance(conversations, list):
            raise SystemExit("Appointment or conversation API returned an invalid response.")

        if verify_booking:
            customer_phone = normalize_phone(required("E2E_CUSTOMER_PHONE"))
            expected_service = required("E2E_EXPECTED_SERVICE_NAME").casefold()
            service_names = {
                item["id"]: str(item.get("name", "")).casefold()
                for item in services
                if isinstance(item, dict) and item.get("id")
            }
            matching_appointments = [
                item
                for item in appointments
                if isinstance(item, dict)
                and normalize_phone(str(item.get("customer_phone", ""))) == customer_phone
                and item.get("status") == "confirmed"
                and service_names.get(item.get("service_id")) == expected_service
            ]
            if not matching_appointments:
                raise SystemExit(
                    "No confirmed test-customer appointment exists for the selected service."
                )
            starts = [item.get("starts_at") for item in matching_appointments]
            if len(starts) != len(set(starts)):
                raise SystemExit(
                    "Duplicate confirmed appointments exist for the same test-customer slot."
                )

            matching_conversations = [
                item
                for item in conversations
                if isinstance(item, dict)
                and normalize_phone(str(item.get("customer_phone", ""))) == customer_phone
            ]
            if len(matching_conversations) != 1:
                raise SystemExit(
                    "Expected exactly one WhatsApp conversation for the test customer."
                )

            conversation_id = matching_conversations[0]["id"]
            messages = request_json(
                client,
                "GET",
                f"/whatsapp/conversations/{conversation_id}/messages?limit=100",
                headers=auth_headers,
            )
            confirmation_found = any(
                isinstance(item, dict)
                and item.get("direction") == "outbound"
                and "appointment is confirmed"
                in str(item.get("body", "")).casefold()
                for item in messages
            )
            if not confirmation_found:
                raise SystemExit(
                    "The WhatsApp thread does not contain the booking confirmation reply."
                )

    print("SmeOps live API smoke checks passed.")
    print(
        "Authentication, database readiness, worker heartbeat, services, hours, "
        "WhatsApp connection and tenant APIs verified."
    )
    if verify_booking:
        print(
            "Confirmed customer appointment and outbound conversation confirmation verified."
        )
    else:
        print(
            "Customer booking assertion skipped; set E2E_VERIFY_CUSTOMER_BOOKING=true "
            "after the test customer completes a real WhatsApp booking."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
