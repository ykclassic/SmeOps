import hashlib
import hmac
import json
import logging
from collections.abc import Iterator
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import DbSession, require_roles
from app.core.config import get_settings
from app.core.whatsapp import encrypt_whatsapp_secret
from app.jobs.queue import enqueue_job
from app.models import AuditEvent, Tenant, User, WhatsAppConnection, WhatsAppWebhookEvent
from app.schemas.whatsapp import (
    WhatsAppConnectionProvision,
    WhatsAppConnectionResponse,
    WhatsAppOutboundMessageResponse,
    WhatsAppTextMessageRequest,
)
from app.services.whatsapp_outbound import (
    OutboundIdempotencyConflict,
    enqueue_whatsapp_text,
)

router = APIRouter(tags=["whatsapp integration"])
logger = logging.getLogger(__name__)\nManagerOrOwner = Annotated[User, Depends(require_roles("owner", "manager"))]


def _extract_event_payloads(payload: dict) -> Iterator[tuple[str, str, str, dict]]:
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata")
            if not isinstance(metadata, dict):
                continue
            phone_number_id = metadata.get("phone_number_id")
            if (
                not isinstance(phone_number_id, str)
                or not phone_number_id.isdigit()
                or not 5 <= len(phone_number_id) <= 80
            ):
                continue

            messages = value.get("messages", [])
            if isinstance(messages, list):
                for message in messages:
                    if (
                        not isinstance(message, dict)
                        or not isinstance(message.get("id"), str)
                        or not 1 <= len(message["id"]) <= 240
                    ):
                        continue
                    yield (
                        phone_number_id,
                        f"message:{message['id']}",
                        "message.received",
                        {"metadata": {"phone_number_id": phone_number_id}, "message": message},
                    )

            statuses = value.get("statuses", [])
            if isinstance(statuses, list):
                for message_status in statuses:
                    if (
                        not isinstance(message_status, dict)
                        or not isinstance(message_status.get("id"), str)
                        or not isinstance(message_status.get("status"), str)
                    ):
                        continue
                    status_name = message_status["status"]
                    provider_event_id = f"status:{message_status['id']}:{status_name}"
                    if len(provider_event_id) > 255:
                        continue
                    yield (
                        phone_number_id,
                        provider_event_id,
                        "message.status",
                        {
                            "metadata": {"phone_number_id": phone_number_id},
                            "status": message_status,
                        },
                    )


@router.get("/webhooks/whatsapp", response_class=PlainTextResponse)
async def verify_whatsapp_webhook(request: Request) -> PlainTextResponse:
    verify_token = get_settings().whatsapp_verify_token
    if verify_token is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WhatsApp webhook verification is not configured",
        )

    mode = request.query_params.get("hub.mode")
    supplied_token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if (
        mode != "subscribe"
        or supplied_token is None
        or challenge is None
        or not hmac.compare_digest(
            supplied_token.encode(),
            verify_token.get_secret_value().encode(),
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Webhook verification failed",
        )
    return PlainTextResponse(challenge)


@router.post("/webhooks/whatsapp", status_code=status.HTTP_200_OK)
async def receive_whatsapp_webhook(
    request: Request,
    session: DbSession,
) -> dict[str, int | str]:
    settings = get_settings()
    app_secret = settings.whatsapp_app_secret
    if app_secret is None or settings.whatsapp_token_encryption_key is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WhatsApp webhook security is not configured",
        )

    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Webhook too large",
        )

    signature = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(
        app_secret.get_secret_value().encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        ) from None
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported webhook payload",
        )

    queued_events = 0
    for phone_number_id, provider_event_id, event_type, event_payload in _extract_event_payloads(
        payload
    ):
        connection_result = await session.execute(
            select(WhatsAppConnection).where(
                WhatsAppConnection.phone_number_id == phone_number_id,
                WhatsAppConnection.status == "active",
            )
        )
        connection = connection_result.scalar_one_or_none()
        if connection is None:
            continue

        canonical_payload = json.dumps(
            event_payload,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_hash = hashlib.sha256(canonical_payload.encode()).hexdigest()
        existing_result = await session.execute(
            select(WhatsAppWebhookEvent).where(
                WhatsAppWebhookEvent.tenant_id == connection.tenant_id,
                WhatsAppWebhookEvent.provider_event_id == provider_event_id,
            )
        )
        existing = existing_result.scalar_one_or_none()
        if existing is not None:
            if existing.payload_sha256 != payload_hash:
                logger.warning(
                    "WhatsApp duplicate event payload hash mismatch",
                    extra={"event_type": event_type},
                )
            continue

        event = WhatsAppWebhookEvent(
            id=uuid4(),
            tenant_id=connection.tenant_id,
            provider_event_id=provider_event_id,
            event_type=event_type,
            payload_encrypted=encrypt_whatsapp_secret(canonical_payload),
            payload_sha256=payload_hash,
            status="queued",
        )
        try:
            async with session.begin_nested():
                session.add(event)
                await session.flush()
        except IntegrityError:
            duplicate_result = await session.execute(
                select(WhatsAppWebhookEvent).where(
                    WhatsAppWebhookEvent.tenant_id == connection.tenant_id,
                    WhatsAppWebhookEvent.provider_event_id == provider_event_id,
                )
            )
            duplicate = duplicate_result.scalar_one_or_none()
            if duplicate is not None and duplicate.payload_sha256 == payload_hash:
                continue
            logger.warning(
                "WhatsApp webhook event insert conflict",
                extra={"event_type": event_type},
            )
            continue

        await enqueue_job(
            session,
            tenant_id=connection.tenant_id,
            job_type="whatsapp.accept_inbound",
            payload={"webhook_event_id": str(event.id)},
            idempotency_key=f"wa:{hashlib.sha256(provider_event_id.encode()).hexdigest()}",
            max_attempts=3,
        )
        queued_events += 1

    await session.commit()
    return {"status": "accepted", "queued_events": queued_events}


@router.post(
    "/internal/whatsapp/connections",
    response_model=WhatsAppConnectionResponse,
    status_code=status.HTTP_200_OK,
    include_in_schema=False,
)
async def provision_whatsapp_connection(
    payload: WhatsAppConnectionProvision,
    session: DbSession,
    admin_key: Annotated[str | None, Header(alias="X-Platform-Admin-Key")] = None,
) -> WhatsAppConnectionResponse:
    settings = get_settings()
    expected_key = settings.platform_admin_key
    if expected_key is None or settings.whatsapp_token_encryption_key is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WhatsApp connection provisioning is not configured",
        )
    if admin_key is None or not hmac.compare_digest(
        admin_key.encode(),
        expected_key.get_secret_value().encode(),
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    tenant_result = await session.execute(
        select(Tenant).where(
            Tenant.id == payload.tenant_id,
            Tenant.status == "active",
        )
    )
    tenant = tenant_result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Business not found")

    connection_result = await session.execute(
        select(WhatsAppConnection)
        .where(WhatsAppConnection.tenant_id == tenant.id)
        .with_for_update()
    )
    connection = connection_result.scalar_one_or_none()
    if connection is None:
        connection = WhatsAppConnection(
            tenant_id=tenant.id,
            phone_number_id=payload.phone_number_id,
            business_account_id=payload.business_account_id,
            access_token_encrypted=encrypt_whatsapp_secret(payload.access_token.get_secret_value()),
            status="active",
        )
    else:
        connection.phone_number_id = payload.phone_number_id
        connection.business_account_id = payload.business_account_id
        connection.access_token_encrypted = encrypt_whatsapp_secret(
            payload.access_token.get_secret_value()
        )
        connection.status = "active"

    session.add(connection)
    session.add(
        AuditEvent(
            tenant_id=tenant.id,
            actor_user_id=None,
            event_type="whatsapp.connection.provisioned",
            subject_type="tenant",
            subject_id=tenant.id,
            payload={"phone_number_id": payload.phone_number_id},
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="WhatsApp phone number is already assigned or connection is invalid",
        ) from exc

    return WhatsAppConnectionResponse(
        tenant_id=tenant.id,
        phone_number_id=connection.phone_number_id,
        business_account_id=connection.business_account_id,
        status=connection.status,
    )



@router.post(
    "/whatsapp/messages",
    response_model=WhatsAppOutboundMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def queue_whatsapp_text_message(
    payload: WhatsAppTextMessageRequest,
    session: DbSession,
    user: ManagerOrOwner,
) -> WhatsAppOutboundMessageResponse:
    try:
        message = await enqueue_whatsapp_text(
            session,
            tenant_id=user.tenant_id,
            recipient=payload.recipient,
            body=payload.body,
            idempotency_key=str(payload.idempotency_key),
            actor_user_id=user.id,
        )
    except OutboundIdempotencyConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key was already used for different message content",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    return WhatsAppOutboundMessageResponse(
        id=message.id,
        status=message.status,
    )
