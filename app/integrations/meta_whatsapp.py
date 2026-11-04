from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx


@dataclass(frozen=True)
class MetaAccessToken:
    value: str
    expires_at: datetime | None


@dataclass(frozen=True)
class MetaPhoneNumber:
    display_phone_number: str | None
    verified_name: str | None


class MetaGraphAPIError(RuntimeError):
    def __init__(self, error_code: str, status_code: int) -> None:
        super().__init__("Meta Graph API request failed")
        self.error_code = error_code
        self.status_code = status_code


def _safe_error_code(response: httpx.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error", {})
        code = error.get("code")
        if isinstance(code, int) and 0 <= code <= 999999:
            return f"meta_graph_{code}"
    except (ValueError, TypeError, AttributeError):
        pass
    return f"meta_http_{response.status_code}"


async def _checked_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        response = await client.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
        )
    except httpx.HTTPError as exc:
        raise MetaGraphAPIError("meta_transport_error", 0) from exc
    if response.is_error:
        raise MetaGraphAPIError(
            _safe_error_code(response),
            response.status_code,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise MetaGraphAPIError("meta_invalid_json", response.status_code) from exc
    if not isinstance(payload, dict):
        raise MetaGraphAPIError("meta_invalid_response", response.status_code)
    return payload


async def exchange_embedded_signup_code(
    *,
    api_version: str,
    app_id: str,
    app_secret: str,
    code: str,
) -> MetaAccessToken:
    timeout = httpx.Timeout(12.0, connect=4.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        payload = await _checked_request(
            client,
            "GET",
            f"https://graph.facebook.com/{api_version}/oauth/access_token",
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "code": code,
            },
        )
    access_token = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if not isinstance(access_token, str) or not 20 <= len(access_token) <= 4096:
        raise MetaGraphAPIError("meta_token_missing", 200)
    expires_at = None
    if isinstance(expires_in, int) and 0 < expires_in <= 366 * 24 * 60 * 60:
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    return MetaAccessToken(value=access_token, expires_at=expires_at)


async def verify_phone_number_belongs_to_waba(
    *,
    api_version: str,
    access_token: str,
    waba_id: str,
    phone_number_id: str,
) -> MetaPhoneNumber:
    timeout = httpx.Timeout(12.0, connect=4.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        payload = await _checked_request(
            client,
            "GET",
            f"https://graph.facebook.com/{api_version}/{waba_id}/phone_numbers",
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "fields": "id,display_phone_number,verified_name",
                "limit": "100",
            },
        )
    numbers = payload.get("data")
    if not isinstance(numbers, list):
        raise MetaGraphAPIError("meta_phone_list_invalid", 200)
    for item in numbers:
        if not isinstance(item, dict) or item.get("id") != phone_number_id:
            continue
        display_number = item.get("display_phone_number")
        verified_name = item.get("verified_name")
        return MetaPhoneNumber(
            display_phone_number=(
                display_number if isinstance(display_number, str) else None
            ),
            verified_name=verified_name if isinstance(verified_name, str) else None,
        )
    raise MetaGraphAPIError("meta_phone_not_in_waba", 200)


async def subscribe_app_to_waba(
    *,
    api_version: str,
    access_token: str,
    waba_id: str,
) -> None:
    timeout = httpx.Timeout(12.0, connect=4.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        payload = await _checked_request(
            client,
            "POST",
            f"https://graph.facebook.com/{api_version}/{waba_id}/subscribed_apps",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if payload.get("success") not in {True, "true"}:
        raise MetaGraphAPIError("meta_waba_subscription_failed", 200)


async def register_phone_number(
    *,
    api_version: str,
    access_token: str,
    phone_number_id: str,
    two_step_pin: str,
) -> None:
    timeout = httpx.Timeout(12.0, connect=4.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        payload = await _checked_request(
            client,
            "POST",
            f"https://graph.facebook.com/{api_version}/{phone_number_id}/register",
            headers={"Authorization": f"Bearer {access_token}"},
            json_body={
                "messaging_product": "whatsapp",
                "pin": two_step_pin,
            },
        )
    if payload.get("success") not in {True, "true"}:
        raise MetaGraphAPIError("meta_phone_registration_failed", 200)
