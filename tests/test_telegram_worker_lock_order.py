import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from app.db import session as db
from app.jobs import telegram as telegram_jobs
from app.jobs.queue import ClaimedJob
from app.models import (
    BackgroundJob,
    TelegramConversation,
    TelegramConversationMessage,
    TelegramWebhookEvent,
    Tenant,
)
from app.services import conversation_engine


async def _wait_until_blocked(waiting_pid: int, blocking_pid: int) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while loop.time() < deadline:
        async with db.AsyncSessionFactory() as observer:
            blockers = await observer.scalar(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": waiting_pid}
            )
        if blocking_pid in (blockers or []):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"PostgreSQL did not report backend {waiting_pid} blocked by {blocking_pid}"
    )


@pytest.mark.asyncio
async def test_telegram_inbound_and_conversation_workers_use_tenant_first_lock_order(
    monkeypatch,
):
    """Exercise both workers against PostgreSQL while they target one conversation."""
    tenant_id = uuid4()
    conversation_id = uuid4()
    initial_message_id = uuid4()
    event_id = uuid4()
    chat_id = str(700000000 + (uuid4().int % 100000000))

    async with db.AsyncSessionFactory() as setup:
        async with setup.begin():
            setup.add(
                Tenant(
                    id=tenant_id,
                    name="ADM-012 lock-order test",
                    slug=f"adm012-lock-{uuid4().hex}",
                    status="active",
                    business_timezone="Africa/Lagos",
                )
            )
            await setup.flush()
            setup.add(
                TelegramConversation(
                    id=conversation_id,
                    tenant_id=tenant_id,
                    telegram_user_id=chat_id,
                    telegram_chat_id=chat_id,
                    customer_name="Lock Order Test",
                    status="active",
                    context={},
                    last_inbound_at=datetime.now(UTC),
                )
            )
            setup.add(
                TelegramConversationMessage(
                    id=initial_message_id,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    provider_message_id=f"{chat_id}:1",
                    direction="inbound",
                    body_encrypted="encrypted-hello",
                    delivery_status="received",
                )
            )
            setup.add(
                TelegramWebhookEvent(
                    id=event_id,
                    tenant_id=tenant_id,
                    bot_id="9016622451",
                    provider_update_id=f"adm012-{uuid4().hex}",
                    event_type="message.received",
                    payload_encrypted="encrypted-payload",
                    payload_sha256="a" * 64,
                    status="queued",
                )
            )

    conversation_has_lock = asyncio.Event()
    release_conversation = asyncio.Event()
    worker_pids: dict[str, int] = {}
    original_factory = db.AsyncSessionFactory

    class TrackingSessionContext:
        def __init__(self, worker_name: str):
            self.worker_name = worker_name
            self.session = original_factory()

        async def __aenter__(self):
            session = await self.session.__aenter__()
            worker_pids[self.worker_name] = await session.scalar(
                text("SELECT pg_backend_pid()")
            )
            return session

        async def __aexit__(self, *args):
            return await self.session.__aexit__(*args)

    monkeypatch.setattr(
        db,
        "AsyncSessionFactory",
        lambda: TrackingSessionContext(asyncio.current_task().get_name()),
    )
    monkeypatch.setattr(
        telegram_jobs,
        "AsyncSessionFactory",
        lambda: TrackingSessionContext(asyncio.current_task().get_name()),
    )
    monkeypatch.setattr(
        telegram_jobs,
        "decrypt_telegram_secret",
        lambda _value: json.dumps(
            {
                "update_id": 991,
                "message": {
                    "message_id": 2,
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": int(chat_id), "type": "private"},
                    "from": {
                        "id": int(chat_id),
                        "is_bot": False,
                        "first_name": "Lock",
                    },
                    "text": "hello again",
                },
            }
        ),
    )
    monkeypatch.setattr(
        telegram_jobs,
        "encrypt_telegram_secret",
        lambda value: f"encrypted:{value}",
    )
    monkeypatch.setattr(
        conversation_engine,
        "decrypt_telegram_secret",
        lambda _value: "hello",
    )

    async def pause_during_conversation_processing(*_args, **_kwargs):
        conversation_has_lock.set()
        await release_conversation.wait()
        return [], "Lock order test response"

    async def no_outbound(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        conversation_engine,
        "_service_menu",
        pause_during_conversation_processing,
    )

    async def no_telegram_api_calls(**_kwargs):
        return None

    monkeypatch.setattr(
        conversation_engine,
        "ensure_private_chat_commands",
        no_telegram_api_calls,
    )
    monkeypatch.setattr(conversation_engine, "_send_reply", no_outbound)

    conversation_task = asyncio.create_task(
        conversation_engine.process_telegram_conversation_job(
            ClaimedJob(
                id=uuid4(),
                tenant_id=tenant_id,
                job_type="telegram.process_conversation",
                payload={
                    "conversation_id": str(conversation_id),
                    "message_id": str(initial_message_id),
                },
                attempts=1,
                max_attempts=3,
            )
        ),
        name="conversation-worker",
    )
    inbound_task = None
    try:
        await asyncio.wait_for(conversation_has_lock.wait(), timeout=5)
        conversation_pid = worker_pids["conversation-worker"]

        inbound_task = asyncio.create_task(
            telegram_jobs.accept_inbound_telegram_job(
                ClaimedJob(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    job_type="telegram.accept_inbound",
                    payload={"webhook_event_id": str(event_id)},
                    attempts=1,
                    max_attempts=3,
                )
            ),
            name="inbound-worker",
        )

        deadline = asyncio.get_running_loop().time() + 5
        while "inbound-worker" not in worker_pids and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert "inbound-worker" in worker_pids
        inbound_pid = worker_pids["inbound-worker"]

        # The inbound worker must wait on the tenant mutex, not hold it while
        # waiting for the conversation mutex. This is the deadlock regression.
        await _wait_until_blocked(inbound_pid, conversation_pid)
        assert not inbound_task.done()

        release_conversation.set()
        await asyncio.wait_for(
            asyncio.gather(conversation_task, inbound_task),
            timeout=10,
        )

        async with original_factory() as verify:
            event = await verify.get(TelegramWebhookEvent, event_id)
            assert event is not None and event.status == "processed"
            assert await verify.scalar(
                select(func.count()).select_from(TelegramConversationMessage).where(
                    TelegramConversationMessage.tenant_id == tenant_id,
                    TelegramConversationMessage.conversation_id == conversation_id,
                )
            ) == 2
            assert await verify.scalar(
                select(func.count()).select_from(BackgroundJob).where(
                    BackgroundJob.tenant_id == tenant_id,
                    BackgroundJob.job_type == "telegram.process_conversation",
                )
            ) == 1
    finally:
        release_conversation.set()
        if inbound_task is not None and not inbound_task.done():
            inbound_task.cancel()
        if not conversation_task.done():
            conversation_task.cancel()
        await asyncio.gather(
            conversation_task,
            *( [inbound_task] if inbound_task is not None else [] ),
            return_exceptions=True,
        )
        async with original_factory() as cleanup:
            async with cleanup.begin():
                await cleanup.execute(
                    text("DELETE FROM telegram_outbound_messages WHERE tenant_id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
                await cleanup.execute(
                    text("DELETE FROM background_jobs WHERE tenant_id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
                await cleanup.execute(
                    text("DELETE FROM telegram_webhook_events WHERE tenant_id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
                await cleanup.execute(
                    text("DELETE FROM telegram_conversation_messages WHERE tenant_id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
                await cleanup.execute(
                    text("DELETE FROM telegram_conversations WHERE tenant_id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )
                await cleanup.execute(
                    text("DELETE FROM tenants WHERE id = :tenant_id"),
                    {"tenant_id": tenant_id},
                )



@pytest.mark.asyncio
async def test_telegram_conversation_final_guard_rolls_back_pending_state(
    monkeypatch,
):
    """A tenant becoming inactive at the final guard must discard all pending writes."""
    from types import SimpleNamespace

    from app.db import session as db_session

    tenant_id = uuid4()
    conversation_id = uuid4()
    message_id = uuid4()
    chat_id = str(720000000 + (uuid4().int % 100000000))
    original_factory = db_session.AsyncSessionFactory

    async with original_factory() as setup:
        async with setup.begin():
            setup.add(
                Tenant(
                    id=tenant_id,
                    name="ADM-012 final-guard rollback",
                    slug=f"adm012-final-{uuid4().hex}",
                    status="active",
                    business_timezone="Africa/Lagos",
                )
            )
            await setup.flush()
            setup.add(
                TelegramConversation(
                    id=conversation_id,
                    tenant_id=tenant_id,
                    telegram_user_id=chat_id,
                    telegram_chat_id=chat_id,
                    customer_name="Final Guard Test",
                    status="active",
                    context={"booking": {"step": "before"}},
                    last_inbound_at=datetime.now(UTC),
                )
            )
            setup.add(
                TelegramConversationMessage(
                    id=message_id,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    provider_message_id=f"{chat_id}:1",
                    direction="inbound",
                    body_encrypted="encrypted-hello",
                    delivery_status="received",
                )
            )

    class InactiveTenantResult:
        @staticmethod
        def scalar_one_or_none():
            return SimpleNamespace(status="deactivated")

    class SessionProxy:
        def __init__(self, session):
            self._session = session
            self.tenant_lock_queries = 0

        def __getattr__(self, name):
            return getattr(self._session, name)

        async def execute(self, statement, *args, **kwargs):
            result = await self._session.execute(statement, *args, **kwargs)
            sql = str(statement).lower()
            if "from tenants" in sql and "for update" in sql:
                self.tenant_lock_queries += 1
                if self.tenant_lock_queries == 2:
                    return InactiveTenantResult()
            return result

    class SessionContext:
        def __init__(self):
            self.session = original_factory()
            self.proxy = SessionProxy(self.session)

        async def __aenter__(self):
            await self.session.__aenter__()
            return self.proxy

        async def __aexit__(self, *args):
            return await self.session.__aexit__(*args)

    monkeypatch.setattr(db_session, "AsyncSessionFactory", SessionContext)
    monkeypatch.setattr(
        conversation_engine,
        "decrypt_telegram_secret",
        lambda _value: "hello",
    )

    async def no_telegram_setup(**_kwargs):
        return None

    async def outbound_must_not_be_enqueued(*_args, **_kwargs):
        pytest.fail("Final lifecycle guard must run before outbound enqueue")

    monkeypatch.setattr(
        conversation_engine,
        "ensure_private_chat_commands",
        no_telegram_setup,
    )
    monkeypatch.setattr(
        conversation_engine,
        "_send_reply",
        outbound_must_not_be_enqueued,
    )

    try:
        await conversation_engine.process_telegram_conversation_job(
            ClaimedJob(
                id=uuid4(),
                tenant_id=tenant_id,
                job_type="telegram.process_conversation",
                payload={
                    "conversation_id": str(conversation_id),
                    "message_id": str(message_id),
                },
                attempts=1,
                max_attempts=3,
            )
        )

        async with original_factory() as verify:
            conversation = await verify.get(TelegramConversation, conversation_id)
            assert conversation is not None
            assert conversation.context == {"booking": {"step": "before"}}
            assert await verify.scalar(
                select(func.count()).select_from(BackgroundJob).where(
                    BackgroundJob.tenant_id == tenant_id
                )
            ) == 0
    finally:
        async with original_factory() as cleanup:
            async with cleanup.begin():
                await cleanup.execute(
                    text("DELETE FROM telegram_outbound_messages WHERE tenant_id = :id"),
                    {"id": tenant_id},
                )
                await cleanup.execute(
                    text("DELETE FROM background_jobs WHERE tenant_id = :id"),
                    {"id": tenant_id},
                )
                await cleanup.execute(
                    text("DELETE FROM telegram_conversation_messages WHERE tenant_id = :id"),
                    {"id": tenant_id},
                )
                await cleanup.execute(
                    text("DELETE FROM telegram_conversations WHERE tenant_id = :id"),
                    {"id": tenant_id},
                )
                await cleanup.execute(
                    text("DELETE FROM tenants WHERE id = :id"),
                    {"id": tenant_id},
                )



@pytest.mark.asyncio
async def test_telegram_deactivation_during_command_provisioning_discards_work(
    monkeypatch,
):
    """A tenant deactivated during Telegram API setup is rejected before processing."""
    tenant_id = uuid4()
    conversation_id = uuid4()
    message_id = uuid4()
    chat_id = str(730000000 + (uuid4().int % 100000000))
    original_factory = db.AsyncSessionFactory
    provisioning_started = asyncio.Event()
    release_provisioning = asyncio.Event()

    async with original_factory() as setup:
        async with setup.begin():
            setup.add(
                Tenant(
                    id=tenant_id,
                    name="ADM-012 provisioning lifecycle race",
                    slug=f"adm012-provision-{uuid4().hex}",
                    status="active",
                    business_timezone="Africa/Lagos",
                )
            )
            await setup.flush()
            setup.add(
                TelegramConversation(
                    id=conversation_id,
                    tenant_id=tenant_id,
                    telegram_user_id=chat_id,
                    telegram_chat_id=chat_id,
                    customer_name="Provisioning Race Test",
                    status="active",
                    context={"booking": {"step": "before"}},
                    last_inbound_at=datetime.now(UTC),
                )
            )
            setup.add(
                TelegramConversationMessage(
                    id=message_id,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    provider_message_id=f"{chat_id}:1",
                    direction="inbound",
                    body_encrypted="encrypted-hello",
                    delivery_status="received",
                )
            )

    async def pause_during_external_provisioning(**_kwargs):
        provisioning_started.set()
        await release_provisioning.wait()

    monkeypatch.setattr(
        conversation_engine, "ensure_private_chat_commands",
        pause_during_external_provisioning,
    )
    monkeypatch.setattr(
        conversation_engine, "decrypt_telegram_secret",
        lambda value: "test-platform-token" if value else "",
    )
    monkeypatch.setattr(
        conversation_engine, "_send_reply",
        lambda *_args, **_kwargs: pytest.fail(
            "Inactive tenant must not enqueue a Telegram reply"
        ),
    )

    try:
        task = asyncio.create_task(
            conversation_engine.process_telegram_conversation_job(
                ClaimedJob(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    job_type="telegram.process_conversation",
                    payload={
                        "conversation_id": str(conversation_id),
                        "message_id": str(message_id),
                    },
                    attempts=1,
                    max_attempts=3,
                )
            )
        )
        await asyncio.wait_for(provisioning_started.wait(), timeout=5)

        async with original_factory() as deactivate:
            async with deactivate.begin():
                tenant = await deactivate.get(Tenant, tenant_id)
                assert tenant is not None
                tenant.status = "deactivated"

        release_provisioning.set()
        await asyncio.wait_for(task, timeout=10)

        async with original_factory() as verify:
            conversation = await verify.get(TelegramConversation, conversation_id)
            assert conversation is not None
            assert conversation.context == {"booking": {"step": "before"}}
            assert await verify.scalar(
                select(func.count()).select_from(BackgroundJob).where(
                    BackgroundJob.tenant_id == tenant_id
                )
            ) == 0
    finally:
        release_provisioning.set()
        async with original_factory() as cleanup:
            async with cleanup.begin():
                await cleanup.execute(
                    text("DELETE FROM telegram_outbound_messages WHERE tenant_id = :id"),
                    {"id": tenant_id},
                )
                await cleanup.execute(
                    text("DELETE FROM background_jobs WHERE tenant_id = :id"),
                    {"id": tenant_id},
                )
                await cleanup.execute(
                    text("DELETE FROM telegram_conversation_messages WHERE tenant_id = :id"),
                    {"id": tenant_id},
                )
                await cleanup.execute(
                    text("DELETE FROM telegram_conversations WHERE tenant_id = :id"),
                    {"id": tenant_id},
                )
                await cleanup.execute(
                    text("DELETE FROM tenants WHERE id = :id"),
                    {"id": tenant_id},
                )
