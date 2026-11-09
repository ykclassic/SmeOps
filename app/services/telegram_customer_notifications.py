from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TelegramConversation, TelegramCustomerIdentity, TelegramPlatformBot
from app.services.telegram_outbound import enqueue_telegram_text


async def enqueue_customer_appointment_notification(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    customer_id: UUID,
    body: str,
    idempotency_key: str,
) -> bool:
    """Queue a customer message only for a verified, active Telegram identity.

    The caller owns the transaction so this queue row and its job commit atomically
    with the appointment mutation and audit event.
    """
    platform_result = await session.execute(
        select(TelegramPlatformBot).where(
            TelegramPlatformBot.singleton_key == "platform",
            TelegramPlatformBot.status == "active",
        )
    )
    platform_bot = platform_result.scalar_one_or_none()
    if platform_bot is None:
        return False

    identity_result = await session.execute(
        select(TelegramCustomerIdentity).where(
            TelegramCustomerIdentity.tenant_id == tenant_id,
            TelegramCustomerIdentity.customer_id == customer_id,
            TelegramCustomerIdentity.platform_bot_id == platform_bot.id,
            TelegramCustomerIdentity.revoked_at.is_(None),
        )
    )
    identity = identity_result.scalar_one_or_none()
    if identity is None:
        return False

    conversation_result = await session.execute(
        select(TelegramConversation).where(
            TelegramConversation.tenant_id == tenant_id,
            TelegramConversation.telegram_user_id == identity.telegram_user_id,
            TelegramConversation.telegram_chat_id == identity.telegram_chat_id,
            TelegramConversation.status != "closed",
        )
    )
    conversation = conversation_result.scalar_one_or_none()
    if conversation is None:
        return False

    try:
        await enqueue_telegram_text(
            session,
            tenant_id=tenant_id,
            conversation_id=conversation.id,
            body=body,
            idempotency_key=idempotency_key,
        )
    except ValueError:
        # The appointment lifecycle remains authoritative. An unavailable or
        # conflicting recipient/message is recorded by the caller as skipped.
        return False
    return True
