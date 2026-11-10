import hashlib
import hmac
import logging

from sqlalchemy import select

from app.core.config import get_settings
from app.core.telegram import (
    decrypt_telegram_secret,
    hash_telegram_webhook_secret,
)
from app.db.session import AsyncSessionFactory
from app.integrations.telegram_bot import (
    TelegramAPIError,
    TelegramDeliveryUnknown,
    get_bot_identity,
    get_webhook_info,
    register_webhook,
    set_bot_commands,
    ensure_private_chat_commands,
)
from app.models import TelegramConversation, TelegramPlatformBot, TelegramOwnerLink

logger = logging.getLogger(__name__)
EXPECTED_PLATFORM_BOT_ID = "9016622451"
EXPECTED_PLATFORM_BOT_USERNAME = "smeops_bot"
PLATFORM_WEBHOOK_PATH = "/webhooks/telegram/platform"


def platform_webhook_secret(bot_id: str) -> str:
    key = get_settings().app_secret_key.get_secret_value().encode()
    return hmac.new(
        key,
        f"smeops:telegram:webhook:{bot_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


async def ensure_platform_webhook(platform_bot: TelegramPlatformBot) -> None:
    settings = get_settings()
    if not settings.public_api_base_url:
        raise RuntimeError("PUBLIC_API_BASE_URL is not configured")
    if (
        platform_bot.bot_id != EXPECTED_PLATFORM_BOT_ID
        or platform_bot.bot_username != EXPECTED_PLATFORM_BOT_USERNAME
        or platform_bot.status != "active"
    ):
        raise RuntimeError("Stored platform Telegram bot identity is not the approved identity")

    token = decrypt_telegram_secret(platform_bot.bot_token_encrypted)
    identity = await get_bot_identity(bot_token=token)
    if (
        identity.bot_id != EXPECTED_PLATFORM_BOT_ID
        or identity.username != EXPECTED_PLATFORM_BOT_USERNAME
    ):
        raise RuntimeError("Telegram getMe identity does not match the configured platform bot")

    expected_secret = platform_webhook_secret(platform_bot.bot_id)
    expected_hash = hash_telegram_webhook_secret(expected_secret)
    secret_changed = not hmac.compare_digest(
        platform_bot.webhook_secret_hash,
        expected_hash,
    )
    if secret_changed:
        platform_bot.webhook_secret_hash = expected_hash

    webhook_url = f"{settings.public_api_base_url}{PLATFORM_WEBHOOK_PATH}"
    info = await get_webhook_info(bot_token=token)
    if secret_changed or info.url != webhook_url:
        await register_webhook(
            bot_token=token,
            webhook_url=webhook_url,
            secret_token=expected_secret,
        )
        info = await get_webhook_info(bot_token=token)
        if info.url != webhook_url:
            raise RuntimeError("Telegram did not confirm the shared webhook URL")

    await set_bot_commands(bot_token=token)

    logger.info(
        "Shared Telegram webhook and command menu reconciled",
        extra={
            "bot_id": platform_bot.bot_id,
            "webhook_url": info.url,
            "pending_update_count": info.pending_update_count,
            "last_error_date": info.last_error_date,
            "has_last_error": info.last_error_date is not None,
        },
    )


async def reconcile_platform_webhook_on_startup() -> None:
    try:
        async with AsyncSessionFactory() as session:
            result = await session.execute(
                select(TelegramPlatformBot).where(
                    TelegramPlatformBot.singleton_key == "platform",
                    TelegramPlatformBot.status == "active",
                )
            )
            platform_bot = result.scalar_one_or_none()
            if platform_bot is None:
                logger.info(
                    "Shared Telegram webhook reconciliation skipped: "
                    "no active platform bot"
                )
                return
            await ensure_platform_webhook(platform_bot)
            token = decrypt_telegram_secret(platform_bot.bot_token_encrypted)
            conversation_chats = await session.execute(
                select(TelegramConversation.telegram_chat_id)
                .where(TelegramConversation.tenant_id.is_not(None))
                .distinct()
            )
            owner_chats = await session.execute(
                select(TelegramOwnerLink.chat_id).where(
                    TelegramOwnerLink.chat_id.is_not(None),
                    TelegramOwnerLink.linked_at.is_not(None),
                    TelegramOwnerLink.disabled_at.is_(None),
                ).distinct()
            )
            chat_ids = {
                str(value)
                for value in conversation_chats.scalars().all()
                if value is not None
            }
            chat_ids.update(
                str(value)
                for value in owner_chats.scalars().all()
                if value is not None
            )
            reconciled_chats = 0
            failed_chats = 0
            for chat_id in sorted(chat_ids):
                try:
                    await ensure_private_chat_commands(
                        bot_token=token,
                        bot_id=platform_bot.bot_id,
                        chat_id=chat_id,
                        force=True,
                    )
                    reconciled_chats += 1
                except (TelegramAPIError, TelegramDeliveryUnknown, ValueError):
                    failed_chats += 1
                    logger.exception(
                        "Telegram private-chat command reconciliation failed",
                        extra={"bot_id": platform_bot.bot_id},
                    )
            await session.commit()
            logger.info(
                "Telegram private-chat command reconciliation completed",
                extra={
                    "bot_id": platform_bot.bot_id,
                    "known_private_chats": len(chat_ids),
                    "reconciled_private_chats": reconciled_chats,
                    "failed_private_chats": failed_chats,
                },
            )
    except (TelegramAPIError, TelegramDeliveryUnknown, RuntimeError, ValueError):
        logger.exception("Shared Telegram webhook reconciliation failed")
    except Exception:
        logger.exception("Unexpected shared Telegram webhook reconciliation failure")
