# Telegram Customer Contact Confirmation Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Require explicit self-shared Telegram contact confirmation before a Telegram booking can proceed, without trusting legacy conversation phone data.

**Architecture:** Extend the existing Telegram booking state machine in app/services/conversation_engine.py with contact-request, contact-confirmation, alternative-number, and verified-contact transitions. Reuse TelegramCustomerIdentity, tenant-scoped Customer records, existing encrypted contact persistence, and reply-keyboard helpers. Do not change WhatsApp behavior.

**Tech Stack:** Python, FastAPI, SQLAlchemy async sessions, Telegram Bot API, pytest.

**Spec:** docs/superpowers/specs/2026-11-10-telegram-contact-confirmation-design.md

## Global Constraints
- Free resources only; do not add an SMS gateway, OTP provider, paid service, or dependency.
- OTP verification for alternative numbers is deferred.
- Manually entered alternative numbers remain unverified and cannot authorize appointment management.
- A verified Telegram contact must have contact.user_id equal to conversation.telegram_user_id.
- Customer and identity operations must be tenant-scoped and fail closed on conflicts.
- Preserve WhatsApp behavior and shared Telegram platform-bot architecture.
- Do not merge to main or deploy to Render without explicit user authorization.

## Review Focus
- Legacy encrypted conversation phone but no verified identity: still request Telegram contact (Task 1).
- Contact belongs to another Telegram user: reject without persisting proof or identity (Tasks 1–2).
- Existing customer phone belongs to another tenant: never reuse it (Task 2).
- Existing Telegram identity conflicts with accepted phone/customer: fail closed without rebinding (Task 2).
- Duplicate contact confirmation or replayed webhook: no duplicate customer/identity creation (Tasks 2–3).

---

### Task 1: Add explicit contact-confirmation states to Telegram booking

**Files**
- Modify: app/services/conversation_engine.py
- Test: tests/test_telegram_booking_conversation.py
- Test: tests/test_telegram_customer_management.py

**Interfaces**
- Consumes: existing _normalize_contact_phone(value: str) -> str | None, _verify_telegram_customer_contact(...), contact_request_keyboard(), and remove_reply_keyboard().
- Produces: persisted booking states awaiting_contact, confirming_contact, awaiting_alternative_phone, and awaiting_date; booking context keys pending_contact_phone and customer_phone. The booking phone may be set from an alternative number but must carry an explicit unverified state; only accepted contact identity establishment marks it verified.

- [ ] Step 1: Write regression tests for legacy-phone bypass and contact request. Prove a Telegram booking with pre-existing conversation.customer_phone_encrypted but no TelegramCustomerIdentity enters awaiting_contact and emits a contact-request keyboard. Assert the old phone is not copied into booking identity or used to skip the prompt.
- [ ] Step 2: Run the focused regression test and confirm failure. Run: pytest tests/test_telegram_booking_conversation.py -k 'legacy_phone or contact_request' -q. Expected: FAIL because current code advances when the old phone exists.
- [ ] Step 3: Write tests for contact receipt and Yes/No decisions. Assert a self-shared valid contact is normalized and displayed, state becomes confirming_contact, Yes advances only after identity establishment, and No enters awaiting_alternative_phone. Assert contacts from another Telegram sender are rejected.
- [ ] Step 4: Run the new state-transition tests and confirm failure. Run: pytest tests/test_telegram_booking_conversation.py -k 'contact_confirmation or alternative_phone' -q. Expected: FAIL because current handler has no confirmation state.
- [ ] Step 5: Implement state transitions in process_channel_conversation_job. For Telegram only, after a valid booking name set awaiting_contact and send contact_request_keyboard(). In awaiting_contact, accept only a contact whose user ID matches the sender, normalize it, save pending_contact_phone, display it, set confirming_contact, and offer Yes/No choices. In confirming_contact, Yes delegates to Task 2 identity resolution; No clears pending contact and requests an alternative number. Alternative phone input is validated and retained only as unverified booking data; it must not update customer_phone_encrypted, create identity, or unlock management. Remove the contact keyboard when the prompt is resolved.
- [ ] Step 6: Remove legacy phone as an onboarding shortcut. Ensure Telegram awaiting_name never branches on decrypted customer_phone. Keep WhatsApp branch unchanged. Telegram booking cannot enter awaiting_date until accepted verified contact identity exists.
- [ ] Step 7: Run focused tests. Run: pytest tests/test_telegram_booking_conversation.py tests/test_telegram_customer_management.py -q. Expected: PASS; update old tests that encode unsafe legacy behavior to the approved flow, not weaken them.
- [ ] Step 8: Commit Task 1. Commit message: feat(telegram): require explicit customer contact confirmation.

### Task 2: Resolve accepted contact to a safe customer and verified identity

**Files**
- Modify: app/services/conversation_engine.py
- Test: tests/test_telegram_customer_management.py
- Test: tests/test_telegram_booking_conversation.py

**Interfaces**
- Consumes: pending_contact_phone from Task 1 and existing TelegramCustomerIdentity, Customer, and active TelegramPlatformBot models.
- Produces: an identity-resolution helper in conversation_engine.py that accepts tenant, conversation, normalized phone, and booking name; returns the resolved Customer only when verified identity is safely established, otherwise fails closed.

- [ ] Step 1: Write tests for existing customer match and safe new customer creation. Assert tenant-scoped existing customer reuse; if absent, create a Customer with confirmed name and phone using established repository conventions. Assert identity binds tenant, customer, platform bot, Telegram user, and chat.
- [ ] Step 2: Run identity-resolution tests and confirm failure. Run: pytest tests/test_telegram_customer_management.py -k 'accepted_contact or create_customer or tenant_isolation' -q. Expected: FAIL because _verify_telegram_customer_contact currently returns None when no customer exists.
- [ ] Step 3: Add conflict and replay tests. Cover an existing identity bound to another customer, unique-constraint races, inactive/missing platform bot, duplicate inbound message, and repeated Yes confirmation. Assert no identity is rebound and no duplicate customer is created.
- [ ] Step 4: Implement the identity-resolution helper. Use tenant-scoped customer lookup; create a new customer only if absent and only within the current transaction. Lock/read existing identity by tenant and Telegram user, reject customer mismatch, and enforce uniqueness constraints for tenant/customer/chat. Set verified_at only after a sender-owned contact is explicitly confirmed. Never use manual alternative phone input here.
- [ ] Step 5: Remove deferred identity creation at appointment confirmation. Delete the contact_self_shared post-booking identity-linking path. Identity must exist before booking advances to date selection, not after an appointment has been created.
- [ ] Step 6: Run focused customer and booking tests. Run: pytest tests/test_telegram_customer_management.py tests/test_telegram_booking_conversation.py -q. Expected: PASS, including tenant isolation and conflict tests.
- [ ] Step 7: Commit Task 2. Commit message: fix(telegram): bind confirmed contacts to tenant customers.

### Task 3: Enforce management authorization and complete lifecycle regression coverage

**Files**
- Modify: app/services/conversation_engine.py only if tests expose authorization gaps
- Test: tests/test_telegram_booking_conversation.py
- Test: tests/test_telegram_customer_management.py

**Interfaces**
- Consumes: verified TelegramCustomerIdentity created by Task 2.
- Produces: acceptance coverage proving appointment list, cancellation, and rescheduling remain inaccessible without an active verified identity.

- [ ] Step 1: Write tests for unverified alternative-number management denial. Simulate No → alternative number and then /appointments, /cancel, and /reschedule; assert each requires Share My Contact and no appointment data or action is exposed.
- [ ] Step 2: Run management-denial tests. Run: pytest tests/test_telegram_booking_conversation.py -k 'unverified_alternative or management_requires_identity' -q. Expected: existing management checks pass; investigate any missing identity guard in follow-up state handling.
- [ ] Step 3: Add complete booking-path integration tests. Exercise service → name → contact share → display/Yes → date → slot → booking confirmation. Add No → alternative number and verify no identity is created. Include stale stored phone, contact from another sender, invalid number, tenant isolation, and duplicate delivery.
- [ ] Step 4: Run all Telegram tests. Run: pytest tests/test_telegram_booking_conversation.py tests/test_telegram_customer_management.py -q. Expected: PASS.
- [ ] Step 5: Run full backend test suite and static checks. Inspect workflow definitions and run documented backend CI commands; expected all required jobs green.
- [ ] Step 6: Review final diff for security and compatibility. Confirm WhatsApp code paths unchanged, no manual number treated as verified, all identity queries tenant-scoped, and no new dependency or paid service.
- [ ] Step 7: Commit Task 3. Commit message: test(telegram): cover contact onboarding and identity boundaries.

### Task 4: Documentation and delivery evidence

**Files**
- Modify: README.md only if customer-facing Telegram onboarding documentation describes the old flow.

- [ ] Step 1: Update only documentation that is inaccurate. Explain Telegram booking requires sharing and confirming the customer's own contact; alternative numbers remain unverified until OTP is introduced.
- [ ] Step 2: Run CI and inspect all job results. Expected: all required workflow jobs pass on feature branch.
- [ ] Step 3: Open a pull request to main. Include state-machine changes, security guarantees, test evidence, and explicit note that no Render deployment or merge has been performed.
- [ ] Step 4: Report exact status separately: code completion, focused tests, full CI, PR status, merge status, and Render staging status. Do not describe CI as proof of live Telegram acceptance.
