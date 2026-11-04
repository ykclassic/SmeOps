# Salon booking conversation

The first conversational workflow is intentionally bounded to salon appointments.

## Customer journey

1. A customer sends a WhatsApp message.
2. The webhook verifies Meta's signature, persists the event and queues work.
3. The inbound worker stores the customer message and queues a conversation-processing job in the same transaction.
4. The conversation worker loads tenant-owned services and guides the customer through service selection, name, date, available time and explicit confirmation.
5. The booking service recalculates the requested appointment against current business hours and confirmed appointments while holding the tenant row lock.
6. The service creates the customer and appointment, writes an audit event, and queues the confirmation response transactionally.
7. Outbound messages are subject to the existing 24-hour customer-service-window and opt-out checks.

## AI boundary

When `OPENAI_API_KEY` and `OPENAI_MODEL` are configured, the OpenAI Responses API is used only to classify an unstructured message as a booking request, human escalation request, yes/no response, or other. The response uses strict JSON Schema output. If the model is unavailable or not configured, deterministic keyword fallback remains available.

The model cannot access the database, choose a slot, create an appointment, set prices, or send a message. Application code validates every state transition and invokes the deterministic booking service. A booking is never created until the customer replies YES to the displayed service, date and time.

## Booking state

Conversation context stores the current flow step, selected service, customer name, requested date, offered UTC slots, selected start time, booking attempt ID, and last processed inbound message timestamp/ID. It is tenant-scoped inside the encrypted-at-rest conversation table.

Supported date inputs are today, tomorrow, weekday names, ISO dates (YYYY-MM-DD), and DD/MM/YYYY. The slot finder currently offers up to three options on a requested day at 30-minute start intervals. Each service duration must fit inside the configured business hours. Confirmed appointments block overlapping slots.

## Limitations in this increment

- No staff/resource-specific calendars; the salon is modeled as one shared appointment resource.
- No split shifts or multiple opening intervals per weekday.
- No payment collection, deposits, reminders, cancellation or rescheduling through WhatsApp yet.
- Human escalation marks the conversation for staff follow-up; staff assignment and dashboard resolution actions are still required.
- The deterministic flow is English-first; multilingual interpretation needs dedicated evaluation.
- The optional model adapter requires a valid OpenAI API key and model name. The test suite must mock the API and must not depend on a live model call.
- This draft has no live Meta account delivery evidence.
