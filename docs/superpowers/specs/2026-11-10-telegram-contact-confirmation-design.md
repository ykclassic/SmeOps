# Telegram Customer Contact Confirmation — Design

**Date:** 2026-11-10  
**Status:** Approved by user  
**Repository:** ykclassic/SmeOps

## Intent
Require every Telegram customer booking to explicitly share and confirm a self-owned Telegram contact. An old encrypted conversation phone number is not evidence of ownership and must never bypass onboarding.

## User experience
After the customer supplies the booking name, SmeOps requests the native Telegram contact. When a valid self-shared contact arrives, it displays the normalized number and asks the customer to confirm: “Yes, use this number” or “No, use another number”.

On Yes, validate Telegram sender ownership, perform tenant-scoped customer matching or safe customer creation, establish the verified Telegram customer identity, and continue booking.

On No, request an alternative phone number. It may be used to complete the appointment booking and stored as unverified booking data, but it must not establish a Telegram identity or authorize appointment history, cancellation, or rescheduling. OTP verification is deferred until an affordable SMS provider is available.

## Goals
- Explicit contact request and confirmation for Telegram booking.
- Only a Telegram contact whose contact.user_id matches the sender can establish verified identity.
- Safe tenant-scoped customer reuse or creation.
- Legacy customer_phone_encrypted values never bypass contact confirmation.
- Appointment management remains gated by an active, non-revoked TelegramCustomerIdentity.
- Preserve WhatsApp behavior and shared platform-bot architecture.

## Non-goals
- SMS/OTP implementation or paid infrastructure.
- Changes to WhatsApp booking and identity flows.
- Automatic merge to main or Render deployment.

## Security and data rules
- Manual alternative numbers are unverified; no identity linkage or management access.
- If an unverified alternative number matches an existing tenant customer, the booking must not overwrite that customer's stored name.
- A contact belonging to another Telegram user is rejected.
- Customer lookup and creation are scoped to tenant_id.
- Identity uniqueness/conflict conditions fail closed.
- Duplicate webhook/message processing must not create duplicate customers or identities.
- Existing Telegram identities must be checked for tenant/user/customer/chat conflicts before mutation.

## Approaches considered
1. Native Telegram contact first (selected): request, display, and confirm the actual Telegram-shared number; establish identity only after explicit Yes. This proves control of the Telegram account's own contact share without paid infrastructure.
2. Confirm a previously stored number first: less reliable because a stored number is not proof of current ownership and would require a separate verification mechanism.

## Implementation decisions
- Insert contact confirmation after booking name collection and before date/availability collection.
- Use explicit persisted booking states for awaiting contact, confirming shared contact, awaiting alternative number, and awaiting date.
- Keep pending and selected phone numbers encrypted in booking context; decrypt only when needed to resolve identity or create the appointment.
- Reuse existing Telegram identity and tenant-scoped Customer models; add no table or dependency.
