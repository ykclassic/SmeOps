import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.whatsapp import decrypt_whatsapp_secret
from app.integrations.meta_whatsapp import (
    MetaGraphAPIError,
    register_phone_number,
    subscribe_app_to_waba,
    verify_phone_number_belongs_to_waba,
)
from app.models import AuditEvent, WhatsAppConnection

logger = logging.getLogger(__name__)


async def complete_pending_whatsapp_connection(
    session: AsyncSession,
    *,
    connection: WhatsAppConnection,
    actor_user_id: UUID | None,
    api_version: str,
) -> WhatsAppConnection:
    access_token = decrypt_whatsapp_secret(connection.access_token_encrypted)
    encrypted_pin = connection.two_step_verification_pin_encrypted
    if encrypted_pin is None:
        raise ValueError("Pending WhatsApp connection is missing its registration PIN")
    two_step_pin = decrypt_whatsapp_secret(encrypted_pin)

    try:
        phone = await verify_phone_number_belongs_to_waba(
            api_version=api_version,
            access_token=access_token,
            waba_id=connection.business_account_id,
            phone_number_id=connection.phone_number_id,
        )
        await subscribe_app_to_waba(
            api_version=api_version,
            access_token=access_token,
            waba_id=connection.business_account_id,
        )
        await register_phone_number(
            api_version=api_version,
            access_token=access_token,
            phone_number_id=connection.phone_number_id,
            two_step_pin=two_step_pin,
        )
    except MetaGraphAPIError as exc:
        connection.status = "pending"
        connection.onboarding_error_code = exc.error_code
        session.add(
            AuditEvent(
                tenant_id=connection.tenant_id,
                actor_user_id=actor_user_id,
                event_type="whatsapp.connection.onboarding_failed",
                subject_type="whatsapp_connection",
                subject_id=connection.id,
                payload={
                    "error_code": exc.error_code,
                    "http_status": exc.status_code,
                },
            )
        )
        await session.commit()
        logger.warning(
            "Meta WhatsApp onboarding step failed",
            extra={
                "tenant_id": str(connection.tenant_id),
                "error_code": exc.error_code,
                "http_status": exc.status_code,
            },
        )
        raise

    connection.display_phone_number = phone.display_phone_number
    connection.verified_name = phone.verified_name
    connection.status = "active"
    connection.onboarding_error_code = None
    session.add(
        AuditEvent(
            tenant_id=connection.tenant_id,
            actor_user_id=actor_user_id,
            event_type="whatsapp.connection.connected",
            subject_type="whatsapp_connection",
            subject_id=connection.id,
            payload={
                "phone_number_id": connection.phone_number_id,
                "business_account_id": connection.business_account_id,
            },
        )
    )
    await session.commit()
    return connection
