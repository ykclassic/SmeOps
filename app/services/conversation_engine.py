import logging
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.telegram import decrypt_telegram_secret, encrypt_telegram_secret
from app.core.whatsapp import decrypt_whatsapp_secret
from app.integrations.conversation_ai import (
    ConversationAIUnavailable,
    classify_inbound_intent,
)
from app.integrations.telegram_bot import (
    contact_request_keyboard,
    ensure_private_chat_commands,
    remove_reply_keyboard,
)
from app.jobs.queue import ClaimedJob
from app.models import (
    Appointment,
    AuditEvent,
    Customer,
    Service,
    TelegramConversation,
    TelegramConversationMessage,
    TelegramCustomerIdentity,
    TelegramPlatformBot,
    Tenant,
    WhatsAppConversation,
    WhatsAppConversationMessage,
)
from app.services.salon_booking import (
    BookingConflict,
    create_appointment_booking,
    get_available_slots,
    parse_booking_date,
)
from app.services.telegram_outbound import enqueue_telegram_text
from app.services.telegram_owner_notifications import enqueue_appointment_owner_alerts
from app.services.whatsapp_outbound import (
    OutboundPolicyViolation,
    enqueue_whatsapp_text,
)

logger = logging.getLogger(__name__)

YES_WORDS = {"yes", "y", "confirm", "confirmed", "book it", "please book"}
NO_WORDS = {"no", "n", "cancel", "no thanks"}
HUMAN_WORDS = {"human", "agent", "staff", "talk to someone", "support"}
START_WORDS = {"hi", "hello", "hey", "book", "booking", "appointment", "appointments"}
PHONE_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")


def _normalize_contact_phone(value: str) -> str | None:
    candidate = re.sub(r"[\s().-]", "", value)
    if candidate.startswith("0") and len(candidate) == 11:
        candidate = "+234" + candidate[1:]
    elif candidate.startswith("234") and len(candidate) == 13:
        candidate = "+" + candidate
    return candidate if PHONE_PATTERN.fullmatch(candidate) else None


async def _verify_telegram_customer_contact(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    conversation: TelegramConversation,
    contact: dict,
    create_customer: bool = False,
    customer_name: str | None = None,
) -> Customer | None:
    sender_id = str(conversation.telegram_user_id)
    if str(contact.get("user_id", "")) != sender_id:
        return None
    phone = _normalize_contact_phone(str(contact.get("phone_number", "")))
    if phone is None:
        return None

    customer_result = await session.execute(
        select(Customer).where(
            Customer.tenant_id == tenant_id,
            Customer.phone_e164 == phone,
        )
    )
    customer = customer_result.scalar_one_or_none()

    bot_result = await session.execute(
        select(TelegramPlatformBot).where(
            TelegramPlatformBot.singleton_key == "platform",
            TelegramPlatformBot.status == "active",
        )
    )
    platform_bot = bot_result.scalar_one_or_none()
    if platform_bot is None:
        return None

    identity_result = await session.execute(
        select(TelegramCustomerIdentity).where(
            TelegramCustomerIdentity.tenant_id == tenant_id,
            TelegramCustomerIdentity.telegram_user_id == sender_id,
        ).with_for_update()
    )
    identity = identity_result.scalar_one_or_none()

    chat_identity_result = await session.execute(
        select(TelegramCustomerIdentity).where(
            TelegramCustomerIdentity.tenant_id == tenant_id,
            TelegramCustomerIdentity.telegram_chat_id == conversation.telegram_chat_id,
        ).with_for_update()
    )
    chat_identity = chat_identity_result.scalar_one_or_none()
    if chat_identity is not None and (
        identity is None or chat_identity.id != identity.id
    ):
        return None

    if customer is None:
        # An existing Telegram identity cannot be rebound to a new phone/customer
        # just because a different contact was shared during another booking.
        if identity is not None or not create_customer or not customer_name:
            return None
        customer = Customer(
            tenant_id=tenant_id,
            name=customer_name,
            phone_e164=phone,
        )
        try:
            async with session.begin_nested():
                session.add(customer)
                await session.flush()
        except IntegrityError:
            customer_result = await session.execute(
                select(Customer).where(
                    Customer.tenant_id == tenant_id,
                    Customer.phone_e164 == phone,
                )
            )
            customer = customer_result.scalar_one_or_none()
            if customer is None:
                return None

    customer_identity_result = await session.execute(
        select(TelegramCustomerIdentity).where(
            TelegramCustomerIdentity.tenant_id == tenant_id,
            TelegramCustomerIdentity.customer_id == customer.id,
        ).with_for_update()
    )
    customer_identity = customer_identity_result.scalar_one_or_none()
    if customer_identity is not None and (
        identity is None or customer_identity.id != identity.id
    ):
        return None

    if identity is not None:
        if (
            identity.customer_id != customer.id
            or identity.telegram_chat_id != conversation.telegram_chat_id
        ):
            return None
        identity.platform_bot_id = platform_bot.id
        identity.revoked_at = None
        identity.verified_at = datetime.now(UTC)
    else:
        identity = TelegramCustomerIdentity(
            tenant_id=tenant_id,
            customer_id=customer.id,
            platform_bot_id=platform_bot.id,
            telegram_user_id=sender_id,
            telegram_chat_id=conversation.telegram_chat_id,
            verified_at=datetime.now(UTC),
        )
        session.add(identity)
    conversation.customer_phone_encrypted = encrypt_telegram_secret(phone)
    return customer


def _contact_confirmation_keyboard() -> dict:
    return {
        "keyboard": [[
            {"text": "Yes, use this number"},
            {"text": "No, use another number"},
        ]],
        "resize_keyboard": True,
        "one_time_keyboard": True,
        "input_field_placeholder": "Confirm your contact number",
    }


def _format_price(price_minor: int) -> str:
    naira = price_minor / 100
    return f"₦{naira:,.2f}"


async def _service_menu(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    business_name: str | None = None,
) -> tuple[list[Service], str]:
    result = await session.execute(
        select(Service)
        .where(Service.tenant_id == tenant_id, Service.is_active.is_(True))
        .order_by(Service.name)
    )
    services = list(result.scalars().all())
    if not services:
        business_label = f"{business_name} has" if business_name else "This business has"
        return services, (
            f"Welcome to {business_name}. " if business_name else ""
        ) + (
            f"{business_label} not published its services yet. "
            "Please contact the business team for assistance."
        )
    lines = []
    if business_name:
        lines.append(f"Welcome to {business_name}.")
    lines.append("Here are the available services:")
    for index, service in enumerate(services, start=1):
        lines.append(
            f"{index}. {service.name} — {_format_price(service.price_minor)} "
            f"({service.duration_minutes} minutes)"
        )
    lines.append("Reply with the number or exact service name to continue.")
    return services, "\n".join(lines)


async def _send_reply(
    session: AsyncSession,
    *,
    channel: str,
    tenant_id: UUID,
    conversation_id: UUID,
    customer_phone: str | None,
    inbound_message_id: UUID,
    body: str,
    reply_markup: dict | None = None,
) -> None:
    if channel == "telegram":
        await enqueue_telegram_text(
            session,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            body=body,
            idempotency_key=f"conversation-reply:{inbound_message_id}",
            reply_markup=reply_markup,
        )
        return
    if customer_phone is None:
        raise ValueError("WhatsApp customer phone is required")
    await enqueue_whatsapp_text(
        session,
        tenant_id=tenant_id,
        recipient=customer_phone,
        body=body,
        idempotency_key=f"conversation-reply:{inbound_message_id}",
    )


async def process_channel_conversation_job(
    job: ClaimedJob,
    *,
    channel: str,
) -> None:
    if channel not in {"whatsapp", "telegram"}:
        raise ValueError("Unsupported conversation channel")
    try:
        conversation_id = UUID(job.payload["conversation_id"])
        message_id = UUID(job.payload["message_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Invalid conversation processing job payload") from exc

    from app.db.session import AsyncSessionFactory

    conversation_model = (
        TelegramConversation if channel == "telegram" else WhatsAppConversation
    )
    message_model = (
        TelegramConversationMessage if channel == "telegram" else WhatsAppConversationMessage
    )
    decrypt_secret = (
        decrypt_telegram_secret if channel == "telegram" else decrypt_whatsapp_secret
    )

    async with AsyncSessionFactory() as session:
        if channel == "telegram":
            lifecycle_result = await session.execute(
                select(Tenant).where(Tenant.id == job.tenant_id)
            )
            lifecycle_tenant = lifecycle_result.scalar_one_or_none()
            if lifecycle_tenant is None:
                raise ValueError("Telegram conversation tenant does not exist")
            if lifecycle_tenant.status != "active":
                logger.info(
                    "Telegram conversation job skipped for inactive tenant",
                    extra={
                        "tenant_id": str(job.tenant_id),
                        "tenant_status": lifecycle_tenant.status,
                    },
                )
                return

        conversation_result = await session.execute(
            select(conversation_model)
            .where(
                conversation_model.id == conversation_id,
                conversation_model.tenant_id == job.tenant_id,
            )
            .with_for_update()
        )
        conversation = conversation_result.scalar_one_or_none()
        if conversation is None:
            raise ValueError("Conversation does not exist")

        message_result = await session.execute(
            select(message_model).where(
                message_model.id == message_id,
                message_model.conversation_id == conversation_id,
                message_model.tenant_id == job.tenant_id,
                message_model.direction == "inbound",
            )
        )
        inbound = message_result.scalar_one_or_none()
        if inbound is None or (
            inbound.body_encrypted is None
            and not getattr(inbound, "contact_phone_encrypted", None)
        ):
            return

        if channel == "telegram":
            platform_result = await session.execute(
                select(TelegramPlatformBot).where(
                    TelegramPlatformBot.singleton_key == "platform",
                    TelegramPlatformBot.status == "active",
                )
            )
            platform_bot = platform_result.scalar_one_or_none()
            if platform_bot is None:
                raise RuntimeError("Active SmeOps Telegram platform bot is unavailable")
            await ensure_private_chat_commands(
                bot_token=decrypt_telegram_secret(platform_bot.bot_token_encrypted),
                bot_id=platform_bot.bot_id,
                chat_id=conversation.telegram_chat_id,
            )

        if (
            (channel == "whatsapp" and conversation.is_opted_out)
            or conversation.status in {"human_required", "closed"}
        ):
            return

        context = dict(conversation.context or {})
        reply_markup = None
        previous_processed = context.get("last_processed_at")
        if previous_processed:
            try:
                previous_at = datetime.fromisoformat(previous_processed)
            except (TypeError, ValueError):
                previous_at = None
            if previous_at is not None and inbound.created_at <= previous_at:
                return

        body = (
            decrypt_secret(inbound.body_encrypted).strip()
            if inbound.body_encrypted else ""
        )
        telegram_contact = None
        if channel == "telegram" and inbound.contact_phone_encrypted:
            telegram_contact = {
                "user_id": inbound.contact_user_id,
                "phone_number": decrypt_telegram_secret(
                    inbound.contact_phone_encrypted
                ),
            }
            body = "[Telegram contact shared]"
        if not body and telegram_contact is None:
            return
        normalized = " ".join(body.casefold().split())
        tenant = await session.get(Tenant, job.tenant_id)
        if tenant is None:
            raise ValueError("Business tenant does not exist")
        timezone = ZoneInfo(tenant.business_timezone)
        customer_phone = None
        if channel == "whatsapp":
            customer_phone = decrypt_whatsapp_secret(
                conversation.customer_phone_encrypted
            )
        elif channel == "telegram":
            # A legacy encrypted phone is not proof of Telegram contact ownership.
            customer_phone = None
        reply: str
        booking = dict(context.get("booking") or {})
        ai_intent = None
        if not booking.get("step") and normalized not in START_WORDS and telegram_contact is None:
            try:
                ai_intent = await classify_inbound_intent(body)
            except ConversationAIUnavailable as exc:
                logger.warning(
                    "Conversation intent interpreter unavailable; using deterministic fallback",
                    extra={
                        "error_type": (
                            type(exc.__cause__).__name__ if exc.__cause__ else "unknown"
                        )
                    },
                )

        if (
            normalized in HUMAN_WORDS
            or (
                ai_intent is not None
                and ai_intent.confidence >= 0.7
                and ai_intent.intent == "human_request"
            )
            or any(
                phrase in normalized
                for phrase in ("speak to a person", "real person", "human agent")
            )
        ):
            conversation.status = "human_required"
            context["booking"] = {}
            reply = (
                "I have flagged this conversation for the salon team. "
                "A staff member will follow up as soon as possible."
            )
        else:
            booking = dict(context.get("booking") or {})
            step = booking.get("step")
            if (
                channel == "telegram"
                and step in {
                    "awaiting_phone",
                    "awaiting_date",
                    "awaiting_slot",
                    "awaiting_confirmation",
                }
                and not booking.get("contact_onboarding_complete")
            ):
                # Migrate legacy in-progress Telegram bookings through the new
                # contact gate instead of trusting a phone saved by the old flow.
                booking.pop("customer_phone", None)
                booking.pop("customer_phone_encrypted", None)
                booking.pop("customer_phone_verified", None)
                booking.pop("contact_self_shared", None)
                booking.pop("pending_contact_phone", None)
                booking.pop("pending_contact_phone_encrypted", None)
                booking["step"] = "awaiting_contact"
                step = "awaiting_contact"

            if telegram_contact is not None and channel == "telegram":
                if booking.get("step") in {
                    "awaiting_contact",
                    "awaiting_phone",
                    "confirming_contact",
                    "awaiting_alternative_phone",
                }:
                    if str(telegram_contact.get("user_id", "")) != str(
                        conversation.telegram_user_id
                    ):
                        reply = (
                            "That contact belongs to a different Telegram account. "
                            "Please share your own contact."
                        )
                        reply_markup = contact_request_keyboard()
                        booking.pop("pending_contact_phone", None)
                        booking.pop("pending_contact_phone_encrypted", None)
                        booking["step"] = "awaiting_contact"
                    else:
                        pending_phone = _normalize_contact_phone(
                            str(telegram_contact.get("phone_number", ""))
                        )
                        if pending_phone is None:
                            reply = (
                                "That contact did not contain a valid phone number. "
                                "Please share your own Telegram contact again."
                            )
                            reply_markup = contact_request_keyboard()
                            booking.pop("pending_contact_phone", None)
                            booking.pop("pending_contact_phone_encrypted", None)
                            booking["step"] = "awaiting_contact"
                        else:
                            booking["pending_contact_phone_encrypted"] = (
                                encrypt_telegram_secret(pending_phone)
                            )
                            booking.pop("pending_contact_phone", None)
                            booking["step"] = "confirming_contact"
                            reply = (
                                f"I received {pending_phone} from your Telegram contact. "
                                "Would you like to use this number for your booking? "
                                "Choose Yes, use this number or No, use another number."
                            )
                            reply_markup = _contact_confirmation_keyboard()
                else:
                    verified_customer = await _verify_telegram_customer_contact(
                        session,
                        tenant_id=job.tenant_id,
                        conversation=conversation,
                        contact=telegram_contact,
                    )
                    if verified_customer is None:
                        reply = (
                            "I couldn't verify that contact for this business. "
                            "Please use the Share My Contact button after starting a booking "
                            "or ask the business team to check your customer record."
                        )
                        reply_markup = contact_request_keyboard()
                    else:
                        customer_phone = verified_customer.phone_e164
                        reply = (
                            f"Your contact is verified for {tenant.name}. "
                            "Use the Telegram menu button to view or manage your appointments."
                        )
                        reply_markup = remove_reply_keyboard()
            elif channel == "telegram" and normalized in {"/help", "help"}:
                reply = (
                    "SmeOps appointment menu:\n"
                    "/book — make an appointment\n"
                    "/appointments — view your upcoming appointments\n"
                    "/cancel — cancel an appointment\n"
                    "/reschedule — change an appointment date or time\n"
                    "/help — show this menu"
                )
            elif channel == "telegram" and normalized in {"/appointments", "my appointments"}:
                context.pop("appointment_management", None)
                identity_result = await session.execute(
                    select(TelegramCustomerIdentity).where(
                        TelegramCustomerIdentity.tenant_id == job.tenant_id,
                        TelegramCustomerIdentity.telegram_user_id == conversation.telegram_user_id,
                        TelegramCustomerIdentity.telegram_chat_id == conversation.telegram_chat_id,
                        TelegramCustomerIdentity.revoked_at.is_(None),
                    )
                )
                identity = identity_result.scalar_one_or_none()
                if identity is None:
                    reply = (
                        "To view your appointments, verify your identity using "
                        "the Share My Contact button below."
                    )
                    reply_markup = contact_request_keyboard()
                else:
                    appointments_result = await session.execute(
                        select(Appointment, Service).join(
                            Service,
                            (Service.id == Appointment.service_id)
                            & (Service.tenant_id == Appointment.tenant_id),
                        ).where(
                            Appointment.tenant_id == job.tenant_id,
                            Appointment.customer_id == identity.customer_id,
                            Appointment.status == "confirmed",
                            Appointment.starts_at >= datetime.now(UTC),
                        ).order_by(Appointment.starts_at).limit(10)
                    )
                    rows = appointments_result.all()
                    if not rows:
                        reply = "You have no upcoming confirmed appointments."
                    else:
                        lines = ["Your upcoming appointments:"]
                        for index, (appointment, service) in enumerate(rows, start=1):
                            local_start = appointment.starts_at.astimezone(timezone)
                            reference = str(appointment.id).replace("-", "")[:8].upper()
                            lines.append(
                                f"{index}. {service.name} — "
                                f"{local_start.strftime('%a, %d %b %Y %I:%M %p')} "
                                f"(Ref: {reference})"
                            )
                        lines.append("Send /cancel or /reschedule to manage an appointment.")
                        reply = "\n".join(lines)

            elif channel == "telegram" and normalized in {"/cancel", "/reschedule"}:
                context.pop("appointment_management", None)
                identity_result = await session.execute(
                    select(TelegramCustomerIdentity).where(
                        TelegramCustomerIdentity.tenant_id == job.tenant_id,
                        TelegramCustomerIdentity.telegram_user_id == conversation.telegram_user_id,
                        TelegramCustomerIdentity.telegram_chat_id == conversation.telegram_chat_id,
                        TelegramCustomerIdentity.revoked_at.is_(None),
                    )
                )
                identity = identity_result.scalar_one_or_none()
                if identity is None:
                    reply = (
                        "Verify your identity using the Share My Contact button below."
                    )
                    reply_markup = contact_request_keyboard()
                else:
                    rows_result = await session.execute(
                        select(Appointment, Service).join(
                            Service,
                            (Service.id == Appointment.service_id)
                            & (Service.tenant_id == Appointment.tenant_id),
                        ).where(
                            Appointment.tenant_id == job.tenant_id,
                            Appointment.customer_id == identity.customer_id,
                            Appointment.status == "confirmed",
                            Appointment.starts_at >= datetime.now(UTC),
                        ).order_by(Appointment.starts_at).limit(10)
                    )
                    rows = rows_result.all()
                    if not rows:
                        reply = "You have no upcoming confirmed appointments to manage."
                    else:
                        action = "cancel" if normalized == "/cancel" else "reschedule"
                        context["appointment_management"] = {
                            "action": action,
                            "step": "choose",
                            "appointment_ids": [str(row[0].id) for row in rows],
                            "customer_id": str(identity.customer_id),
                            "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                        }
                        lines = [f"Choose an appointment to {action} by replying with its number:"]
                        for index, (appointment, service) in enumerate(rows, start=1):
                            local_start = appointment.starts_at.astimezone(timezone)
                            reference = str(appointment.id).replace("-", "")[:8].upper()
                            lines.append(
                                f"{index}. {service.name} — "
                                f"{local_start.strftime('%a, %d %b %Y %I:%M %p')} "
                                f"(Ref: {reference})"
                            )
                        reply = "\n".join(lines)
            elif channel == "telegram" and isinstance(context.get("appointment_management"), dict):
                management = dict(context["appointment_management"])
                management_step = management.get("step")
                action = management.get("action")
                customer_id = management.get("customer_id")
                try:
                    expires_at = datetime.fromisoformat(management.get("expires_at", ""))
                except (TypeError, ValueError):
                    expires_at = None
                if (
                    action not in {"cancel", "reschedule"}
                    or not customer_id
                    or expires_at is None
                    or expires_at <= datetime.now(UTC)
                ):
                    context.pop("appointment_management", None)
                    reply = (
                        "That appointment-management session expired. "
                        "Send /appointments to start again."
                    )
                elif management_step == "choose":
                    choices = management.get("appointment_ids", [])
                    if not normalized.isdigit() or not 1 <= int(normalized) <= len(choices):
                        reply = f"Reply with a number from 1 to {len(choices)}."
                    else:
                        appointment_id = UUID(choices[int(normalized) - 1])
                        owned_result = await session.execute(
                            select(Appointment, Service).join(
                                Service,
                                (Service.id == Appointment.service_id)
                                & (Service.tenant_id == Appointment.tenant_id),
                            ).where(
                                Appointment.id == appointment_id,
                                Appointment.tenant_id == job.tenant_id,
                                Appointment.customer_id == UUID(customer_id),
                                Appointment.status == "confirmed",
                                Appointment.starts_at >= datetime.now(UTC),
                            )
                        )
                        owned = owned_result.one_or_none()
                        if owned is None:
                            context.pop("appointment_management", None)
                            reply = (
                                "That appointment is no longer available to manage. "
                                "Send /appointments to refresh."
                            )
                        else:
                            appointment, service = owned
                            management["appointment_id"] = str(appointment.id)
                            if action == "cancel":
                                management["step"] = "confirm_cancel"
                                appointment_local = appointment.starts_at.astimezone(timezone)
                                appointment_label = appointment_local.strftime(
                                    "%A, %d %B at %I:%M %p"
                                )
                                reply = (
                                    f"Cancel {service.name} on {appointment_label}? "
                                    "Reply YES to confirm or NO to keep it."
                                )
                            else:
                                management["step"] = "await_date"
                                management["old_starts_at"] = appointment.starts_at.isoformat()
                                reply = (
                                    "What new date would you like? Send today, tomorrow, "
                                    "a weekday, or YYYY-MM-DD."
                                )
                            context["appointment_management"] = management
                elif management_step == "confirm_cancel":
                    if normalized in NO_WORDS:
                        context.pop("appointment_management", None)
                        reply = "Cancellation stopped. Your appointment remains confirmed."
                    elif normalized in YES_WORDS:
                        await session.execute(
                            select(Tenant).where(Tenant.id == job.tenant_id).with_for_update()
                        )
                        result = await session.execute(
                            select(Appointment, Service).join(
                                Service,
                                (Service.id == Appointment.service_id)
                                & (Service.tenant_id == Appointment.tenant_id),
                            ).where(
                                Appointment.id == UUID(management["appointment_id"]),
                                Appointment.tenant_id == job.tenant_id,
                                Appointment.customer_id == UUID(customer_id),
                                Appointment.status == "confirmed",
                                Appointment.starts_at >= datetime.now(UTC),
                            ).with_for_update(of=Appointment)
                        )
                        row = result.one_or_none()
                        if row is None:
                            reply = "That appointment can no longer be cancelled."
                        else:
                            appointment, service = row
                            now = datetime.now(UTC)
                            event_id = uuid4()
                            appointment.status = "cancelled"
                            appointment.cancelled_at = now
                            session.add(AuditEvent(
                                id=event_id,
                                tenant_id=job.tenant_id,
                                actor_user_id=None,
                                event_type="appointment.cancelled.customer",
                                subject_type="appointment",
                                subject_id=appointment.id,
                                payload={"cancelled_at": now.isoformat(), "source": "telegram"},
                            ))
                            await enqueue_appointment_owner_alerts(
                                session,
                                tenant_id=job.tenant_id,
                                appointment_id=appointment.id,
                                event_type="appointment.cancelled.customer",
                                event_key=f"appointment.cancelled.customer:{event_id}",
                            )
                            reply = (
                                "Your appointment has been cancelled. "
                                "The business has been notified."
                            )
                        context.pop("appointment_management", None)
                    else:
                        reply = "Please reply YES to cancel or NO to keep the appointment."
                elif management_step == "await_date":
                    requested_day = parse_booking_date(
                        normalized, timezone=timezone, now=datetime.now(UTC)
                    )
                    if requested_day is None or requested_day < datetime.now(timezone).date():
                        reply = "I couldn't identify a future date. Send a weekday or YYYY-MM-DD."
                    else:
                        appointment_result = await session.execute(
                            select(Appointment, Service).join(
                                Service,
                                (Service.id == Appointment.service_id)
                                & (Service.tenant_id == Appointment.tenant_id),
                            ).where(
                                Appointment.id == UUID(management["appointment_id"]),
                                Appointment.tenant_id == job.tenant_id,
                                Appointment.customer_id == UUID(customer_id),
                                Appointment.status == "confirmed",
                            )
                        )
                        row = appointment_result.one_or_none()
                        if row is None:
                            context.pop("appointment_management", None)
                            reply = "That appointment is no longer available to reschedule."
                        else:
                            appointment, service = row
                            slots = await get_available_slots(
                                session,
                                tenant_id=job.tenant_id,
                                service_id=service.id,
                                day=requested_day,
                                limit=48,
                                exclude_appointment_id=appointment.id,
                            )
                            if not slots:
                                reply = (
                                    "There are no available times on that date. "
                                    "Send another date."
                                )
                            else:
                                management["step"] = "choose_slot"
                                management["requested_date"] = requested_day.isoformat()
                                management["available_slots"] = [slot.isoformat() for slot in slots]
                                context["appointment_management"] = management
                                lines = [f"Available times on {requested_day.isoformat()}:"]
                                lines.extend(
                                    f"{index}. {slot.astimezone(timezone).strftime('%I:%M %p')}"
                                    for index, slot in enumerate(slots, start=1)
                                )
                                lines.append("Reply with the number of your preferred time.")
                                reply = "\n".join(lines)
                elif management_step == "choose_slot":
                    slots = management.get("available_slots", [])
                    if not normalized.isdigit() or not 1 <= int(normalized) <= len(slots):
                        reply = f"Reply with a number from 1 to {len(slots)}."
                    else:
                        selected = datetime.fromisoformat(slots[int(normalized) - 1])
                        management["new_starts_at"] = selected.isoformat()
                        management["step"] = "confirm_reschedule"
                        context["appointment_management"] = management
                        selected_local = selected.astimezone(timezone)
                        selected_label = selected_local.strftime("%A, %d %B %Y at %I:%M %p")
                        old_start = datetime.fromisoformat(
                            management["old_starts_at"]
                        ).astimezone(timezone)
                        old_label = old_start.strftime("%A, %d %B %Y at %I:%M %p")
                        reply = (
                            f"Change from {old_label} to {selected_label}? "
                            "Reply YES to confirm or NO to stop."
                        )
                elif management_step == "confirm_reschedule":
                    if normalized in NO_WORDS:
                        context.pop("appointment_management", None)
                        reply = "Rescheduling stopped. Your original appointment remains unchanged."
                    elif normalized in YES_WORDS:
                        await session.execute(
                            select(Tenant).where(Tenant.id == job.tenant_id).with_for_update()
                        )
                        result = await session.execute(
                            select(Appointment, Service).join(
                                Service,
                                (Service.id == Appointment.service_id)
                                & (Service.tenant_id == Appointment.tenant_id),
                            ).where(
                                Appointment.id == UUID(management["appointment_id"]),
                                Appointment.tenant_id == job.tenant_id,
                                Appointment.customer_id == UUID(customer_id),
                                Appointment.status == "confirmed",
                                Appointment.starts_at >= datetime.now(UTC),
                            ).with_for_update(of=Appointment)
                        )
                        row = result.one_or_none()
                        if row is None:
                            reply = "That appointment can no longer be rescheduled."
                        else:
                            appointment, service = row
                            old_start, old_end = appointment.starts_at, appointment.ends_at
                            new_start = datetime.fromisoformat(
                                management["new_starts_at"]
                            ).astimezone(UTC)
                            requested_day = datetime.fromisoformat(
                                management["requested_date"]
                            ).date()
                            original_start = datetime.fromisoformat(
                                management["old_starts_at"]
                            ).astimezone(UTC)
                            current_slots = await get_available_slots(
                                session,
                                tenant_id=job.tenant_id,
                                service_id=service.id,
                                day=requested_day,
                                limit=48,
                                exclude_appointment_id=appointment.id,
                            )
                            if appointment.starts_at != original_start:
                                reply = (
                                    "This appointment changed after you selected it. "
                                    "Send /reschedule to review the current time."
                                )
                            elif new_start == appointment.starts_at:
                                reply = "That is already your current appointment time."
                            elif new_start not in current_slots:
                                reply = (
                                    "That time is no longer available. "
                                    "Send /reschedule to try again."
                                )
                            else:
                                now = datetime.now(UTC)
                                event_id = uuid4()
                                appointment.starts_at = new_start
                                appointment.ends_at = new_start + timedelta(
                                    minutes=service.duration_minutes
                                )
                                appointment.rescheduled_at = now
                                session.add(AuditEvent(
                                    id=event_id,
                                    tenant_id=job.tenant_id,
                                    actor_user_id=None,
                                    event_type="appointment.rescheduled.customer",
                                    subject_type="appointment",
                                    subject_id=appointment.id,
                                    payload={
                                        "previous_starts_at": old_start.isoformat(),
                                        "previous_ends_at": old_end.isoformat(),
                                        "starts_at": appointment.starts_at.isoformat(),
                                        "ends_at": appointment.ends_at.isoformat(),
                                        "rescheduled_at": now.isoformat(),
                                        "source": "telegram",
                                    },
                                ))
                                await enqueue_appointment_owner_alerts(
                                    session,
                                    tenant_id=job.tenant_id,
                                    appointment_id=appointment.id,
                                    event_type="appointment.rescheduled.customer",
                                    event_key=f"appointment.rescheduled.customer:{event_id}",
                                )
                                new_local = appointment.starts_at.astimezone(timezone)
                                new_label = new_local.strftime("%A, %d %B %Y at %I:%M %p")
                                reply = (
                                    f"Your appointment has been rescheduled to {new_label}. "
                                    "The business has been notified."
                                )
                        context.pop("appointment_management", None)
                    else:
                        reply = "Please reply YES to confirm the new time or NO to stop."
                else:
                    context.pop("appointment_management", None)
                    reply = (
                        "That appointment-management session expired. "
                        "Send /appointments to start again."
                    )
            elif step == "awaiting_service":
                services_result = await session.execute(
                    select(Service)
                    .where(Service.tenant_id == job.tenant_id, Service.is_active.is_(True))
                    .order_by(Service.name)
                )
                services = list(services_result.scalars().all())
                selected = None
                if normalized.isdigit():
                    index = int(normalized) - 1
                    if 0 <= index < len(services):
                        selected = services[index]
                if selected is None:
                    selected = next(
                        (item for item in services if item.name.casefold() == normalized),
                        None,
                    )
                if selected is None:
                    _, reply = await _service_menu(
                        session,
                        job.tenant_id,
                        business_name=tenant.name,
                    )
                else:
                    booking.update(
                        {
                            "step": "awaiting_name",
                            "service_id": str(selected.id),
                            "service_name": selected.name,
                            "service_duration_minutes": selected.duration_minutes,
                            "service_price_minor": selected.price_minor,
                            "booking_attempt_id": str(uuid4()),
                        }
                    )
                    reply = f"You selected {selected.name}. What name should I use for the booking?"
            elif step == "awaiting_name":
                customer_name = " ".join(body.split())
                if len(customer_name) < 2 or len(customer_name) > 160 or any(
                    character.isdigit() for character in customer_name
                ):
                    reply = "Please send the name for the appointment (2–160 characters)."
                else:
                    booking["customer_name"] = customer_name
                    if channel == "telegram":
                        for key in (
                            "customer_phone",
                            "customer_phone_encrypted",
                            "customer_phone_verified",
                            "contact_onboarding_complete",
                            "contact_self_shared",
                            "pending_contact_phone",
                            "pending_contact_phone_encrypted",
                        ):
                            booking.pop(key, None)
                        booking["step"] = "awaiting_contact"
                        reply = (
                            f"Thanks, {customer_name}. Before checking availability, "
                            "please tap Share My Contact below so I can show you the number "
                            "Telegram shares, then ask you to confirm it."
                        )
                        reply_markup = contact_request_keyboard()
                    else:
                        booking["step"] = "awaiting_date"
                        reply = (
                            f"Thanks, {customer_name}. What day would you like to visit? "
                            "Reply with today, tomorrow, a weekday (for example Friday), "
                            "or a date in YYYY-MM-DD format."
                        )
            elif channel == "telegram" and step == "awaiting_contact":
                reply = (
                    "Please tap Share My Contact below. I need the number shared "
                    "by your Telegram account before continuing."
                )
                reply_markup = contact_request_keyboard()
            elif channel == "telegram" and step == "confirming_contact":
                if normalized in YES_WORDS or normalized in {
                    "yes use this number",
                    "yes, use this number",
                }:
                    pending_phone_encrypted = booking.get(
                        "pending_contact_phone_encrypted"
                    )
                    pending_phone = (
                        decrypt_telegram_secret(pending_phone_encrypted)
                        if pending_phone_encrypted
                        else ""
                    )
                    verified_customer = await _verify_telegram_customer_contact(
                        session,
                        tenant_id=job.tenant_id,
                        conversation=conversation,
                        contact={
                            "user_id": conversation.telegram_user_id,
                            "phone_number": pending_phone,
                        },
                        create_customer=True,
                        customer_name=booking.get("customer_name"),
                    )
                    if verified_customer is None:
                        booking.pop("pending_contact_phone", None)
                        booking.pop("pending_contact_phone_encrypted", None)
                        booking["step"] = "awaiting_contact"
                        reply = (
                            "I couldn't safely verify that contact for this business. "
                            "Please share your own Telegram contact again."
                        )
                        reply_markup = contact_request_keyboard()
                    else:
                        booking["customer_phone_encrypted"] = encrypt_telegram_secret(
                            verified_customer.phone_e164
                        )
                        booking.pop("customer_phone", None)
                        booking["customer_phone_verified"] = True
                        booking["contact_onboarding_complete"] = True
                        booking.pop("pending_contact_phone", None)
                        booking.pop("pending_contact_phone_encrypted", None)
                        booking["step"] = "awaiting_date"
                        reply = (
                            f"Thanks, {verified_customer.name}. Your contact is confirmed. "
                            "What day would you like to visit? Reply with today, tomorrow, "
                            "a weekday, or a date in YYYY-MM-DD format."
                        )
                        reply_markup = remove_reply_keyboard()
                elif normalized in NO_WORDS or normalized in {
                    "no use another number",
                    "no, use another number",
                }:
                    booking.pop("pending_contact_phone", None)
                    booking.pop("pending_contact_phone_encrypted", None)
                    booking["step"] = "awaiting_alternative_phone"
                    reply = (
                        "What phone number would you like to use for this booking? "
                        "Send it in international format, for example +2348012345678. "
                        "This alternative number will remain unverified."
                    )
                    reply_markup = remove_reply_keyboard()
                else:
                    reply = (
                        "Please choose Yes, use this number or No, use another number."
                    )
                    reply_markup = _contact_confirmation_keyboard()
            elif channel == "telegram" and step == "awaiting_alternative_phone":
                candidate = _normalize_contact_phone(body.strip())
                if candidate is None:
                    reply = (
                        "Please send a valid phone number in international format, "
                        "for example +2348012345678."
                    )
                else:
                    booking["customer_phone_encrypted"] = encrypt_telegram_secret(
                        candidate
                    )
                    booking.pop("customer_phone", None)
                    booking["customer_phone_verified"] = False
                    booking["contact_onboarding_complete"] = True
                    booking["step"] = "awaiting_date"
                    reply = (
                        "Thanks. That number is saved for this booking but is not verified. "
                        "What day would you like to visit? Reply with today, tomorrow, "
                        "a weekday, or a date in YYYY-MM-DD format."
                    )
            elif step == "awaiting_phone":
                if channel == "telegram":
                    booking["step"] = "awaiting_contact"
                    reply = "Please share your own Telegram contact using the button below."
                    reply_markup = contact_request_keyboard()
                else:
                    candidate = _normalize_contact_phone(body.strip())
                    if candidate is None:
                        reply = (
                            "Please send a valid phone number in international format, "
                            "for example +2348012345678."
                        )
                    else:
                        customer_phone = candidate
                        booking["customer_phone"] = candidate
                        booking["step"] = "awaiting_date"
                        reply = (
                            "Thanks. What day would you like to visit? Reply with today, "
                            "tomorrow, a weekday, or a date in YYYY-MM-DD format."
                        )

            elif step == "awaiting_date":
                requested_day = parse_booking_date(
                    normalized,
                    timezone=timezone,
                    now=datetime.now(UTC),
                )
                if requested_day is None or requested_day < datetime.now(timezone).date():
                    reply = (
                        "I couldn't identify a future date. Please send today, tomorrow, "
                        "a weekday, or a date in YYYY-MM-DD format."
                    )
                else:
                    try:
                        slots = await get_available_slots(
                            session,
                            tenant_id=job.tenant_id,
                            service_id=UUID(booking["service_id"]),
                            day=requested_day,
                            limit=3,
                        )
                    except (BookingConflict, ValueError):
                        slots = []
                    if not slots:
                        reply = (
                            "There are no available appointments on that date. "
                            "Please send another date."
                        )
                    else:
                        booking["requested_date"] = requested_day.isoformat()
                        booking["available_slots"] = [slot.isoformat() for slot in slots]
                        booking["step"] = "awaiting_slot"
                        lines = [f"Available times on {requested_day.isoformat()}:"]
                        lines.extend(
                            f"{index}. {slot.astimezone(timezone).strftime('%I:%M %p')}"
                            for index, slot in enumerate(slots, start=1)
                        )
                        lines.append("Reply with 1, 2 or 3 to select a time.")
                        reply = "\n".join(lines)
            elif step == "awaiting_slot":
                slots = booking.get("available_slots", [])
                if not normalized.isdigit() or not 1 <= int(normalized) <= len(slots):
                    reply = (
                        "Please choose a listed time by replying with a number "
                        f"from 1 to {len(slots)}."
                    )
                else:
                    selected_slot = datetime.fromisoformat(slots[int(normalized) - 1])
                    booking["starts_at"] = selected_slot.isoformat()
                    booking["step"] = "awaiting_confirmation"
                    reply = (
                        f"Please confirm: {booking['service_name']} for {booking['customer_name']} "
                        "on "
                        f"{selected_slot.astimezone(timezone).strftime('%A, %d %B at %I:%M %p')}. "
                        "Reply YES to confirm or NO to start again."
                    )
            elif step == "awaiting_confirmation":
                if channel == "telegram" and not booking.get(
                    "contact_onboarding_complete"
                ):
                    booking["step"] = "awaiting_contact"
                    reply = (
                        "Before I can complete this booking, please share and confirm "
                        "your own Telegram contact using the button below."
                    )
                    reply_markup = contact_request_keyboard()
                elif normalized in YES_WORDS:
                    try:
                        if channel == "telegram":
                            booking_phone_encrypted = booking.get(
                                "customer_phone_encrypted"
                            )
                            booking_phone = (
                                decrypt_telegram_secret(booking_phone_encrypted)
                                if booking_phone_encrypted
                                else None
                            )
                        else:
                            booking_phone = booking.get("customer_phone") or customer_phone
                        if booking_phone and not booking_phone.startswith("+"):
                            booking_phone = "+" + booking_phone
                        if not booking_phone:
                            raise BookingConflict("A verified customer phone number is required")
                        appointment = await create_appointment_booking(
                            session,
                            tenant_id=job.tenant_id,
                            service_id=UUID(booking["service_id"]),
                            customer_name=booking["customer_name"],
                            customer_phone=booking_phone,
                            starts_at=datetime.fromisoformat(booking["starts_at"]),
                            conversation_id=conversation.id,
                            booking_attempt_id=UUID(booking["booking_attempt_id"]),
                            source=channel,
                            update_existing_customer_name=not (
                                channel == "telegram"
                                and booking.get("customer_phone_verified") is False
                            ),
                        )
                    except (BookingConflict, ValueError):
                        booking = {}
                        reply = (
                            "That time is no longer available, so I haven't booked it. "
                            "Reply BOOK to choose another appointment."
                        )
                    else:
                        start_local = appointment.starts_at.astimezone(timezone)
                        booking["step"] = "complete"
                        booking["appointment_id"] = str(appointment.id)
                        reply = (
                            f"Your appointment is confirmed.\n"
                            f"Reference: {str(appointment.id)[:8].upper()}\n"
                            f"Service: {booking['service_name']}\n"
                            f"Date: {start_local.strftime('%A, %d %B %Y')}\n"
                            f"Time: {start_local.strftime('%I:%M %p')}\n"
                            "Thank you for booking with us."
                        )
                elif normalized in NO_WORDS:
                    booking = {}
                    _, reply = await _service_menu(
                        session,
                        job.tenant_id,
                        business_name=tenant.name,
                    )
                    if booking == {}:
                        booking["step"] = "awaiting_service"
                else:
                    reply = "Please reply YES to confirm this appointment or NO to start again."
            elif (
                normalized in START_WORDS
                or any(word in normalized for word in ("book", "appointment", "hair", "salon"))
                or (
                    ai_intent is not None
                    and ai_intent.confidence >= 0.7
                    and ai_intent.intent == "booking_start"
                )
            ):
                _, reply = await _service_menu(
                    session,
                    job.tenant_id,
                    business_name=tenant.name,
                )
                booking = {"step": "awaiting_service"}
            elif not step:
                _, reply = await _service_menu(session, job.tenant_id, business_name=tenant.name)
                booking = {"step": "awaiting_service"}
            else:
                reply = (
                    "I can help book a salon appointment. Reply BOOK to start, "
                    "or HUMAN to speak with the salon team."
                )

            context["booking"] = booking

        context["last_processed_at"] = inbound.created_at.isoformat()
        context["last_processed_message_id"] = str(inbound.id)
        conversation.context = context
        if channel == "telegram":
            # Revalidate under the tenant mutex immediately before committing
            # conversation state and outbound work. Pending ORM writes must not
            # autoflush before this lock is acquired.
            with session.no_autoflush:
                final_tenant_result = await session.execute(
                    select(Tenant).where(Tenant.id == job.tenant_id).with_for_update()
                )
            final_tenant = final_tenant_result.scalar_one_or_none()
            if final_tenant is None:
                await session.rollback()
                raise ValueError("Telegram conversation tenant does not exist")
            if final_tenant.status != "active":
                await session.rollback()
                logger.info(
                    "Telegram conversation result discarded after tenant deactivation",
                    extra={
                        "tenant_id": str(job.tenant_id),
                        "tenant_status": final_tenant.status,
                    },
                )
                return
        try:
            await _send_reply(
                session,
                channel=channel,
                tenant_id=job.tenant_id,
                conversation_id=conversation.id,
                customer_phone=customer_phone,
                inbound_message_id=inbound.id,
                body=reply,
                reply_markup=reply_markup if channel == "telegram" else None,
            )
            await session.commit()
        except OutboundPolicyViolation:
            logger.warning(
                "WhatsApp reply suppressed by messaging policy",
                extra={"tenant_id": str(job.tenant_id), "conversation_id": str(conversation.id)},
            )
            await session.commit()



async def process_whatsapp_conversation_job(job: ClaimedJob) -> None:
    await process_channel_conversation_job(job, channel="whatsapp")


async def process_telegram_conversation_job(job: ClaimedJob) -> None:
    await process_channel_conversation_job(job, channel="telegram")
