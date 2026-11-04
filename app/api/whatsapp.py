import hashlib
import hmac
import json
import logging
from collections.abc import Iterator
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import DbSession, require_roles
from app.core.config import get_settings
from app.core.whatsapp import (
    decrypt_whatsapp_secret,
    encrypt_whatsapp_secret,
)
from app.integrations.meta_whatsapp import (
    MetaGraphAPIError,
    exchange_embedded_signup_code,
)
from app.jobs.queue import enqueue_job
from app.models import (
    AuditEvent,
    BackgroundJob,
    Tenant,
    User,
    WhatsAppConnection,
    WhatsAppConversation,
    WhatsAppConversationMessage,
    WhatsAppWebhookEvent,
)
from app.schemas.whatsapp import (
    WhatsAppConnectionProvision,
    WhatsAppConnectionResponse,
    WhatsAppConversationMessageResponse,
    WhatsAppConversationResponse,
    WhatsAppEmbeddedSignupRequest,
    WhatsAppOutboundMessageResponse,
    WhatsAppTextMessageRequest,
)
from app.services.whatsapp_outbound import (
    OutboundIdempotencyConflict,
    OutboundPolicyViolation,
    enqueue_whatsapp_text,
)

router = APIRouter(tags=["whatsapp integration"])
logger = logging.getLogger(__name__)
ManagerOrOwner = Annotated[
    User, Depends(require_roles("owner", "manager"))
]
OwnerOnly = Annotated[User, Depends(require_roles("owner"))]


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
    "/whatsapp/embedded-signup",
    response_model=WhatsAppConnectionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def complete_embedded_signup(
    request: WhatsAppEmbeddedSignupRequest,
    session: DbSession,
    user: OwnerOnly,
) -> WhatsAppConnectionResponse:
    settings = get_settings()
    if (
        settings.meta_app_id is None
        or settings.meta_app_secret is None
        or settings.whatsapp_graph_api_version is None
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WhatsApp Embedded Signup is not configured",
        )

    other_owner_precheck = await session.execute(
        select(WhatsAppConnection.tenant_id).where(
            WhatsAppConnection.phone_number_id == request.phone_number_id,
            WhatsAppConnection.tenant_id != user.tenant_id,
        )
    )
    if other_owner_precheck.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This WhatsApp number is already connected to another workspace",
        )
    existing_precheck = await session.execute(
        select(WhatsAppConnection)
        .where(WhatsAppConnection.tenant_id == user.tenant_id)
        .with_for_update()
    )
    existing_connection = existing_precheck.scalar_one_or_none()
    if (
        existing_connection is not None
        and existing_connection.status == "active"
        and existing_connection.phone_number_id != request.phone_number_id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Disconnect the current WhatsApp number before connecting a different number",
        )

    if existing_connection is not None and existing_connection.status == "pending":
        active_onboarding = await session.execute(
            select(BackgroundJob.id)
            .where(
                BackgroundJob.tenant_id == user.tenant_id,
                BackgroundJob.job_type == "whatsapp.complete_signup",
                BackgroundJob.status.in_(["queued", "running"]),
                BackgroundJob.idempotency_key.like(
                    f"wa:onboarding%:{existing_connection.id}:%"
                ),
            )
            .limit(1)
        )
        if active_onboarding.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="WhatsApp onboarding is still processing. Retry or wait for the worker to finish.",
            )

    try:
        token = await exchange_embedded_signup_code(
            api_version=settings.whatsapp_graph_api_version,
            app_id=settings.meta_app_id,
            app_secret=settings.meta_app_secret.get_secret_value(),
            code=request.code.get_secret_value(),
        )
    except MetaGraphAPIError as exc:
        logger.warning(
            "Meta authorization code exchange failed",
            extra={
                "tenant_id": str(user.tenant_id),
                "error_code": exc.error_code,
                "http_status": exc.status_code,
            },
        )
        response_status = (
            status.HTTP_400_BAD_REQUEST
            if exc.status_code == 400
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(
            status_code=response_status,
            detail="Meta authorization code could not be exchanged. Restart Embedded Signup.",
        ) from exc

    other_owner_result = await session.execute(
        select(WhatsAppConnection.tenant_id).where(
            WhatsAppConnection.phone_number_id == request.phone_number_id,
            WhatsAppConnection.tenant_id != user.tenant_id,
        )
    )
    if other_owner_result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This WhatsApp number is already connected to another workspace",
        )

    connection_result = await session.execute(
        select(WhatsAppConnection)
        .where(WhatsAppConnection.tenant_id == user.tenant_id)
        .with_for_update()
    )
    connection = connection_result.scalar_one_or_none()
    if (
        connection is not None
        and connection.status == "active"
        and connection.phone_number_id != request.phone_number_id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Disconnect the current WhatsApp number before connecting a different number",
        )

    if connection is None:
        connection = WhatsAppConnection(
            id=uuid4(),
            tenant_id=user.tenant_id,
            phone_number_id=request.phone_number_id,
            business_account_id=request.waba_id,
            access_token_encrypted=encrypt_whatsapp_secret(token.value),
            status="pending",
        )
        session.add(connection)
    else:
        connection.phone_number_id = request.phone_number_id
        connection.business_account_id = request.waba_id
        connection.access_token_encrypted = encrypt_whatsapp_secret(token.value)
        connection.status = "pending"

    connection.display_phone_number = None
    connection.verified_name = None
    connection.token_expires_at = token.expires_at
    connection.two_step_verification_pin_encrypted = encrypt_whatsapp_secret(
        request.two_step_pin
    )
    connection.onboarding_error_code = None
    session.add(
        AuditEvent(
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            event_type="whatsapp.connection.onboarding_started",
            subject_type="whatsapp_connection",
            subject_id=connection.id,
            payload={
                "phone_number_id": request.phone_number_id,
                "business_account_id": request.waba_id,
            },
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This WhatsApp number is already connected to another workspace",
        ) from exc

    code_fingerprint = hashlib.sha256(
        request.code.get_secret_value().encode()
    ).hexdigest()
    await enqueue_job(
        session,
        tenant_id=user.tenant_id,
        job_type="whatsapp.complete_signup",
        payload={"connection_id": str(connection.id)},
        idempotency_key=f"wa:onboarding:{connection.id}:{code_fingerprint}",
        max_attempts=3,
    )
    await session.commit()

    return WhatsAppConnectionResponse(
        tenant_id=connection.tenant_id,
        phone_number_id=connection.phone_number_id,
        business_account_id=connection.business_account_id,
        status=connection.status,
        display_phone_number=connection.display_phone_number,
        verified_name=connection.verified_name,
        token_expires_at=connection.token_expires_at,
        onboarding_error_code=connection.onboarding_error_code,
    )


@router.post(
    "/whatsapp/connection/disconnect",
    response_model=WhatsAppConnectionResponse,
)
async def disable_whatsapp_connection(
    session: DbSession,
    user: OwnerOnly,
) -> WhatsAppConnectionResponse:
    result = await session.execute(
        select(WhatsAppConnection)
        .where(WhatsAppConnection.tenant_id == user.tenant_id)
        .with_for_update()
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="WhatsApp connection not found",
        )
    if connection.status != "disabled":
        connection.status = "disabled"
        session.add(
            AuditEvent(
                tenant_id=user.tenant_id,
                actor_user_id=user.id,
                event_type="whatsapp.connection.disabled",
                subject_type="whatsapp_connection",
                subject_id=connection.id,
                payload={"phone_number_id": connection.phone_number_id},
            )
        )
        await session.commit()
    return WhatsAppConnectionResponse(
        tenant_id=connection.tenant_id,
        phone_number_id=connection.phone_number_id,
        business_account_id=connection.business_account_id,
        status=connection.status,
        display_phone_number=connection.display_phone_number,
        verified_name=connection.verified_name,
        token_expires_at=connection.token_expires_at,
        onboarding_error_code=connection.onboarding_error_code,
    )


@router.post(
    "/whatsapp/connection/complete",
    response_model=WhatsAppConnectionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_pending_whatsapp_connection(
    session: DbSession,
    user: OwnerOnly,
) -> WhatsAppConnectionResponse:
    result = await session.execute(
        select(WhatsAppConnection)
        .where(
            WhatsAppConnection.tenant_id == user.tenant_id,
            WhatsAppConnection.status == "pending",
        )
        .with_for_update()
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="There is no pending WhatsApp connection to complete",
        )

    active_job_result = await session.execute(
        select(BackgroundJob.id)
        .where(
            BackgroundJob.tenant_id == user.tenant_id,
            BackgroundJob.job_type == "whatsapp.complete_signup",
            BackgroundJob.status.in_(["queued", "running"]),
            BackgroundJob.idempotency_key.like(
                f"wa:onboarding%:{connection.id}:%"
            ),
        )
        .limit(1)
    )
    if active_job_result.scalar_one_or_none() is None:
        updated_at = connection.updated_at.isoformat()
        retry_fingerprint = hashlib.sha256(
            f"{connection.id}:{updated_at}".encode()
        ).hexdigest()
        await enqueue_job(
            session,
            tenant_id=user.tenant_id,
            job_type="whatsapp.complete_signup",
            payload={"connection_id": str(connection.id)},
            idempotency_key=(
                f"wa:onboarding-retry:{connection.id}:{retry_fingerprint}"
            ),
            max_attempts=3,
        )
        await session.commit()

    return WhatsAppConnectionResponse(
        tenant_id=connection.tenant_id,
        phone_number_id=connection.phone_number_id,
        business_account_id=connection.business_account_id,
        status=connection.status,
        display_phone_number=connection.display_phone_number,
        verified_name=connection.verified_name,
        token_expires_at=connection.token_expires_at,
        onboarding_error_code=connection.onboarding_error_code,
    )


@router.get(
    "/whatsapp/connection",
    response_model=WhatsAppConnectionResponse | None,
)
async def get_whatsapp_connection(
    session: DbSession,
    user: ManagerOrOwner,
) -> WhatsAppConnectionResponse | None:
    result = await session.execute(
        select(WhatsAppConnection).where(
            WhatsAppConnection.tenant_id == user.tenant_id,
        )
    )
    connection = result.scalar_one_or_none()
    if connection is None:
        return None
    return WhatsAppConnectionResponse(
        tenant_id=connection.tenant_id,
        phone_number_id=connection.phone_number_id,
        business_account_id=connection.business_account_id,
        status=connection.status,
        display_phone_number=connection.display_phone_number,
        verified_name=connection.verified_name,
        token_expires_at=connection.token_expires_at,
        onboarding_error_code=connection.onboarding_error_code,
    )


@router.get(
    "/whatsapp/conversations",
    response_model=list[WhatsAppConversationResponse],
)
async def list_whatsapp_conversations(
    session: DbSession,
    user: ManagerOrOwner,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[WhatsAppConversationResponse]:
    result = await session.execute(
        select(WhatsAppConversation)
        .where(WhatsAppConversation.tenant_id == user.tenant_id)
        .order_by(
            WhatsAppConversation.last_inbound_at.desc(),
            WhatsAppConversation.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return [
        WhatsAppConversationResponse(
            id=conversation.id,
            customer_phone=decrypt_whatsapp_secret(
                conversation.customer_phone_encrypted
            ),
            status=conversation.status,
            last_inbound_at=conversation.last_inbound_at,
            is_opted_out=conversation.is_opted_out,
            opted_out_at=conversation.opted_out_at,
        )
        for conversation in result.scalars().all()
    ]


@router.get(
    "/whatsapp/conversations/{conversation_id}/messages",
    response_model=list[WhatsAppConversationMessageResponse],
)
async def list_whatsapp_conversation_messages(
    conversation_id: UUID,
    session: DbSession,
    user: ManagerOrOwner,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[WhatsAppConversationMessageResponse]:
    conversation_result = await session.execute(
        select(WhatsAppConversation.id).where(
            WhatsAppConversation.id == conversation_id,
            WhatsAppConversation.tenant_id == user.tenant_id,
        )
    )
    if conversation_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    message_result = await session.execute(
        select(WhatsAppConversationMessage)
        .where(
            WhatsAppConversationMessage.conversation_id == conversation_id,
            WhatsAppConversationMessage.tenant_id == user.tenant_id,
        )
        .order_by(
            WhatsAppConversationMessage.created_at.desc(),
            WhatsAppConversationMessage.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return [
        WhatsAppConversationMessageResponse(
            id=message.id,
            direction=message.direction,
            message_type=message.message_type,
            body=(
                decrypt_whatsapp_secret(message.body_encrypted)
                if message.body_encrypted is not None
                else None
            ),
            delivery_status=message.delivery_status,
            occurred_at=message.provider_timestamp or message.created_at,
        )
        for message in message_result.scalars().all()
    ]


@router.post(
    "/whatsapp/conversations/{conversation_id}/resolve",
    status_code=status.HTTP_200_OK,
)
async def resolve_whatsapp_conversation(
    conversation_id: UUID,
    session: DbSession,
    user: ManagerOrOwner,
) -> dict[str, str]:
    result = await session.execute(
        select(WhatsAppConversation)
        .where(
            WhatsAppConversation.id == conversation_id,
            WhatsAppConversation.tenant_id == user.tenant_id,
        )
        .with_for_update()
    )
    conversation = result.scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conversation.is_opted_out:
        raise HTTPException(
            status_code=409,
            detail="An opted-out customer conversation cannot be reopened for automated replies",
        )
    conversation.status = "active"
    session.add(
        AuditEvent(
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            event_type="whatsapp.conversation.resolved",
            subject_type="whatsapp_conversation",
            subject_id=conversation.id,
            payload={},
        )
    )
    await session.commit()
    return {"status": conversation.status}


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
    except OutboundPolicyViolation as exc:
        policy_status = (
            status.HTTP_403_FORBIDDEN
            if exc.error_code == "customer_opted_out"
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(
            status_code=policy_status,
            detail=str(exc),
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
