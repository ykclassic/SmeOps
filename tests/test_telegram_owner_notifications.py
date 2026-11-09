import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.jobs import telegram as telegram_jobs
from app.services import telegram_owner_notifications


class Result:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def all(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value

    def one_or_none(self):
        return self.value


class Session:
    def __init__(self, results):
        self.results = list(results)
        self.records = []
        self.commits = 0

    async def execute(self, _statement):
        return Result(self.results.pop(0))

    def add(self, value):
        self.records.append(value)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_valid_owner_start_token_is_consumed_and_audited():
    token = "short-lived-random-token"
    owner_link = SimpleNamespace(
        id=uuid4(),
        owner_user_id=uuid4(),
        invitation_hash=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        linked_at=None,
        disabled_at=None,
        chat_id=None,
    )
    session = Session([owner_link])

    consumed = await telegram_jobs._consume_owner_link(
        session,
        tenant_id=uuid4(),
        payload={
            "message": {
                "text": f"/start {token}",
                "chat": {"id": 998877, "type": "private"},
                "from": {"id": 998877, "is_bot": False},
            }
        },
    )

    assert consumed is True
    assert owner_link.chat_id == "998877"
    assert owner_link.linked_at is not None
    assert len(session.records) == 1
    assert session.records[0].event_type == "telegram.owner_link.connected"


@pytest.mark.asyncio
async def test_expired_or_unknown_owner_start_token_is_not_linked():
    session = Session([None])

    consumed = await telegram_jobs._consume_owner_link(
        session,
        tenant_id=uuid4(),
        payload={
            "message": {
                "text": "/start invalid-token",
                "chat": {"id": 998877, "type": "private"},
                "from": {"id": 998877, "is_bot": False},
            }
        },
    )

    assert consumed is True
    assert session.records == []


@pytest.mark.asyncio
async def test_owner_link_commands_in_groups_are_not_consumed_as_links():
    session = Session([])

    consumed = await telegram_jobs._consume_owner_link(
        session,
        tenant_id=uuid4(),
        payload={
            "message": {
                "text": "/start token",
                "chat": {"id": -100998877, "type": "group"},
                "from": {"id": 998877, "is_bot": False},
            }
        },
    )

    assert consumed is True
    assert session.records == []


@pytest.mark.asyncio
async def test_appointment_alert_is_queued_once_per_linked_owner(monkeypatch):
    tenant_id = uuid4()
    owner_id = uuid4()
    appointment_id = uuid4()
    link = SimpleNamespace(
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        chat_id="998877",
        linked_at=datetime.now(UTC),
        disabled_at=None,
    )
    session = Session([[link], None])
    jobs = []

    async def enqueue(_session, **kwargs):
        jobs.append(kwargs)

    monkeypatch.setattr(telegram_owner_notifications, "enqueue_job", enqueue)

    count = await telegram_owner_notifications.enqueue_appointment_owner_alerts(
        session,
        tenant_id=tenant_id,
        appointment_id=appointment_id,
    )

    assert count == 1
    assert len(session.records) == 1
    assert jobs[0]["job_type"] == "telegram.owner_appointment_alert"
    assert jobs[0]["tenant_id"] == tenant_id
    assert jobs[0]["idempotency_key"] == (
        f"tg:owner-alert:appointment.created:{appointment_id}:{owner_id}"
    )


@pytest.mark.asyncio
async def test_existing_owner_notification_is_not_queued_again(monkeypatch):
    tenant_id = uuid4()
    owner_id = uuid4()
    appointment_id = uuid4()
    link = SimpleNamespace(
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        chat_id="998877",
        linked_at=datetime.now(UTC),
        disabled_at=None,
    )
    existing = SimpleNamespace(id=uuid4())
    session = Session([[link], existing])
    jobs = []

    async def enqueue(_session, **kwargs):
        jobs.append(kwargs)

    monkeypatch.setattr(telegram_owner_notifications, "enqueue_job", enqueue)

    count = await telegram_owner_notifications.enqueue_appointment_owner_alerts(
        session,
        tenant_id=tenant_id,
        appointment_id=appointment_id,
    )

    assert count == 0
    assert jobs == []
    assert session.records == []



@pytest.mark.asyncio
async def test_owner_link_rejects_private_chat_not_owned_by_sender():
    session = Session([])

    consumed = await telegram_jobs._consume_owner_link(
        session,
        tenant_id=uuid4(),
        payload={
            "message": {
                "text": "/start token",
                "chat": {"id": 998877, "type": "private"},
                "from": {"id": 112233, "is_bot": False},
            }
        },
    )

    assert consumed is True
    assert session.records == []
    assert session.results == []



@pytest.mark.asyncio
async def test_owner_notification_worker_sends_reference_and_local_booking_details(monkeypatch):
    tenant_id = uuid4()
    owner_id = uuid4()
    appointment_id = uuid4()
    notification = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        owner_user_id=owner_id,
        appointment_id=appointment_id,
        event_type="appointment.created",
        status="queued",
        provider_message_id=None,
        last_error_code=None,
        sent_at=None,
    )
    link = SimpleNamespace(chat_id="998877")
    connection = SimpleNamespace(bot_token_encrypted="cipher:bot-token")
    appointment = SimpleNamespace(
        id=appointment_id,
        starts_at=datetime(2026, 11, 9, 9, 0, tzinfo=UTC),
        status="confirmed",
    )
    service = SimpleNamespace(name="Hairs Washing")
    customer = SimpleNamespace(name="Ada Lovelace", phone_e164="+2348012345678")
    tenant = SimpleNamespace(name="Test Salon", business_timezone="Africa/Lagos")

    first = Session([notification, link, connection, (appointment, service, customer, tenant)])
    second = Session([notification])
    sessions = iter([first, second])

    class Context:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_args):
            return False

    from app.jobs import telegram_owner_notifications as notification_jobs
    from app.jobs.queue import ClaimedJob

    sent_messages = []

    async def fake_send(*, bot_token, chat_id, body):
        sent_messages.append((bot_token, chat_id, body))
        return SimpleNamespace(provider_message_id="telegram-message-123")

    monkeypatch.setattr(
        notification_jobs,
        "AsyncSessionFactory",
        lambda: Context(next(sessions)),
    )
    monkeypatch.setattr(
        notification_jobs,
        "decrypt_telegram_secret",
        lambda value: value.removeprefix("cipher:"),
    )
    monkeypatch.setattr(notification_jobs, "send_text_message", fake_send)

    await notification_jobs.send_owner_appointment_alert_job(
        ClaimedJob(
            id=uuid4(),
            tenant_id=tenant_id,
            job_type="telegram.owner_appointment_alert",
            payload={"notification_id": str(notification.id)},
            attempts=1,
            max_attempts=3,
        )
    )

    assert len(sent_messages) == 1
    bot_token, chat_id, body = sent_messages[0]
    assert (bot_token, chat_id) == ("bot-token", "998877")
    assert "Reference: " + str(appointment_id).replace("-", "")[:8].upper() in body
    assert "Service: Hairs Washing" in body
    assert "Customer: Ada Lovelace" in body
    assert "Monday, 09 November 2026" in body
    assert "10:00 AM" in body
    assert "+2348012345678" not in body
    assert notification.status == "sent"
    assert notification.provider_message_id == "998877:telegram-message-123"
    assert notification.sent_at is not None


@pytest.mark.asyncio
async def test_shared_platform_bot_delivers_each_tenant_alert_to_its_own_owner(monkeypatch):
    from app.jobs import telegram_owner_notifications as notification_jobs
    from app.jobs.queue import ClaimedJob

    shared_bot = SimpleNamespace(bot_token_encrypted="cipher:shared-platform-token")
    sessions = []
    expected = []
    for business_name, chat_id in (("Salon A", "111001"), ("Salon B", "222002")):
        tenant_id = uuid4()
        owner_id = uuid4()
        appointment_id = uuid4()
        notification = SimpleNamespace(
            id=uuid4(),
            tenant_id=tenant_id,
            owner_user_id=owner_id,
            appointment_id=appointment_id,
            event_type="appointment.created",
            status="queued",
            provider_message_id=None,
            last_error_code=None,
            sent_at=None,
        )
        link = SimpleNamespace(chat_id=chat_id)
        appointment = SimpleNamespace(
            id=appointment_id,
            starts_at=datetime(2026, 11, 9, 9, 0, tzinfo=UTC),
            status="confirmed",
        )
        service = SimpleNamespace(name=f"{business_name} Service")
        customer = SimpleNamespace(name=f"{business_name} Customer")
        tenant = SimpleNamespace(
            name=business_name,
            business_timezone="Africa/Lagos",
        )
        sessions.extend(
            [
                Session(
                    [notification, link, shared_bot, (appointment, service, customer, tenant)]
                ),
                Session([notification]),
            ]
        )
        expected.append((notification, chat_id, business_name, appointment_id))

    class Context:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_args):
            return False

    sent_messages = []

    async def fake_send(*, bot_token, chat_id, body):
        sent_messages.append((bot_token, chat_id, body))
        return SimpleNamespace(provider_message_id=f"message-{chat_id}")

    session_iterator = iter(sessions)
    monkeypatch.setattr(
        notification_jobs,
        "AsyncSessionFactory",
        lambda: Context(next(session_iterator)),
    )
    monkeypatch.setattr(
        notification_jobs,
        "decrypt_telegram_secret",
        lambda value: value.removeprefix("cipher:"),
    )
    monkeypatch.setattr(notification_jobs, "send_text_message", fake_send)

    for notification, _chat_id, _business_name, _appointment_id in expected:
        await notification_jobs.send_owner_appointment_alert_job(
            ClaimedJob(
                id=uuid4(),
                tenant_id=notification.tenant_id,
                job_type="telegram.owner_appointment_alert",
                payload={"notification_id": str(notification.id)},
                attempts=1,
                max_attempts=3,
            )
        )

    assert len(sent_messages) == 2
    for (bot_token, chat_id, body), (
        notification,
        expected_chat_id,
        business_name,
        appointment_id,
    ) in zip(sent_messages, expected, strict=True):
        assert bot_token == "shared-platform-token"
        assert chat_id == expected_chat_id
        assert f"Business: {business_name}" in body
        assert (
            "Reference: " + str(appointment_id).replace("-", "")[:8].upper()
        ) in body
        assert notification.status == "sent"
