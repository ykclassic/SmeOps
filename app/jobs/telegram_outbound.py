import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from app.core.telegram import decrypt_telegram_secret
from app.db.session import AsyncSessionFactory
from app.integrations.telegram_bot import (
    TelegramAPIError,
    TelegramDeliveryUnknown,
    TelegramSendRejected,
    send_text_message,
)
from app.jobs.queue import ClaimedJob
from app.models import (
    TelegramConversationMessage,
    TelegramOutboundMessage,
    TelegramPlatformBot,
)

logger = logging.getLogger(__name__)


async def send_outbound_telegram_job(job: ClaimedJob) -> None:
    try:
        message_id = UUID(job.payload["message_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid outbound Telegram job payload") from exc

    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(TelegramOutboundMessage)
            .where(
                TelegramOutboundMessage.id == message_id,
                TelegramOutboundMessage.tenant_id == job.tenant_id,
            )
            .with_for_update()
        )
        message = result.scalar_one_or_none()
        if message is None:
            raise ValueError("Outbound Telegram message does not exist")
        if message.status in {"sent", "failed", "unknown"}:
            return
        if message.status == "sending":
            # A prior worker may have sent the request before crashing. Do not
            # repeat a non-idempotent Bot API write after an ambiguous outcome.
            message.status = "unknown"
            message.last_error_code = "previous_send_outcome_unknown"
            await _sync_history(session, job.tenant_id, message)
            await session.commit()
            return
        if message.status != "queued":
            raise ValueError("Outbound Telegram message is not sendable")

        platform_result = await session.execute(
            select(TelegramPlatformBot).where(
                TelegramPlatformBot.singleton_key == "platform",
                TelegramPlatformBot.status == "active",
            )
        )
        platform_bot = platform_result.scalar_one_or_none()
        if platform_bot is None:
            message.status = "failed"
            message.last_error_code = "platform_bot_unavailable"
            await _sync_history(session, job.tenant_id, message)
            await session.commit()
            return

        bot_token = decrypt_telegram_secret(platform_bot.bot_token_encrypted)
        chat_id = message.recipient_chat_id
        body = decrypt_telegram_secret(message.body_encrypted)
        reply_markup = message.reply_markup
        message.status = "sending"
        await session.commit()

    try:
        result = await send_text_message(
            bot_token=bot_token,
            chat_id=chat_id,
            body=body,
            reply_markup=reply_markup,
        )
    except (TelegramAPIError, TelegramSendRejected, ValueError):
        new_status = "failed"
        error_code = "provider_rejected"
        provider_message_id = None
    except TelegramDeliveryUnknown:
        new_status = "unknown"
        error_code = "provider_outcome_unknown"
        provider_message_id = None
    else:
        new_status = "sent"
        error_code = None
        provider_message_id = f"{chat_id}:{result.provider_message_id}"

    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(TelegramOutboundMessage)
            .where(
                TelegramOutboundMessage.id == message_id,
                TelegramOutboundMessage.tenant_id == job.tenant_id,
            )
            .with_for_update()
        )
        message = result.scalar_one_or_none()
        if message is None:
            logger.error(
                "Outbound Telegram ledger row disappeared",
                extra={"message_id": str(message_id)},
            )
            return
        if message.status != "sending":
            logger.warning(
                "Outbound Telegram state changed during provider request",
                extra={"message_id": str(message_id), "status": message.status},
            )
            return
        message.status = new_status
        message.last_error_code = error_code
        message.provider_message_id = provider_message_id
        if new_status == "sent":
            message.sent_at = datetime.now(UTC)
        await _sync_history(session, job.tenant_id, message)
        await session.commit()


async def _sync_history(session, tenant_id: UUID, message: TelegramOutboundMessage) -> None:
    history_result = await session.execute(
        select(TelegramConversationMessage)
        .where(
            TelegramConversationMessage.tenant_id == tenant_id,
            TelegramConversationMessage.provider_message_id == f"outbound:{message.id}",
        )
        .with_for_update()
    )
    history = history_result.scalar_one_or_none()
    if history is not None:
        if message.provider_message_id is not None:
            history.provider_message_id = message.provider_message_id
        history.delivery_status = message.status
