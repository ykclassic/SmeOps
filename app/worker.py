import asyncio
import logging
import signal
import socket
from uuid import uuid4

from sqlalchemy import text

from app.db.session import engine
from app.jobs.telegram import accept_inbound_telegram_job
from app.jobs.telegram_outbound import send_outbound_telegram_job
from app.jobs.whatsapp import accept_inbound_whatsapp_job
from app.jobs.whatsapp_onboarding import complete_whatsapp_onboarding_job
from app.jobs.whatsapp_outbound import send_outbound_whatsapp_job
from app.jobs.worker import run_forever
from app.services.conversation_engine import (
    process_telegram_conversation_job,
    process_whatsapp_conversation_job,
)

logger = logging.getLogger(__name__)

# Stable application-wide key: PostgreSQL session advisory locks are automatically
# released if the database connection or process disappears.
WORKER_ADVISORY_LOCK_KEY = 73_120_261_106
WORKER_LOCK_RETRY_SECONDS = 5.0

HANDLERS = {
    "whatsapp.accept_inbound": accept_inbound_whatsapp_job,
    "telegram.accept_inbound": accept_inbound_telegram_job,
    "telegram.process_conversation": process_telegram_conversation_job,
    "telegram.send_text": send_outbound_telegram_job,
    "whatsapp.complete_signup": complete_whatsapp_onboarding_job,
    "whatsapp.process_conversation": process_whatsapp_conversation_job,
    "whatsapp.send_text": send_outbound_whatsapp_job,
}


async def _wait_or_stop(stop_event: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
    except TimeoutError:
        pass


async def run_embedded_worker(stop_event: asyncio.Event) -> None:
    """Run the durable job consumer while holding the singleton PostgreSQL lock."""
    worker_id = f"smeops-api-{socket.gethostname()[:32]}-{uuid4().hex[:12]}"

    while not stop_event.is_set():
        try:
            async with engine.connect() as lock_connection:
                acquired = await lock_connection.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_key)"),
                    {"lock_key": WORKER_ADVISORY_LOCK_KEY},
                )
                if not acquired:
                    logger.warning(
                        "Another SmeOps worker holds the singleton lock; retrying"
                    )
                    await _wait_or_stop(stop_event, WORKER_LOCK_RETRY_SECONDS)
                    continue

                logger.info("Embedded worker acquired singleton lock")
                try:
                    await run_forever(
                        worker_id=worker_id,
                        handlers=HANDLERS,
                        stop_event=stop_event,
                    )
                finally:
                    try:
                        await lock_connection.execute(
                            text("SELECT pg_advisory_unlock(:lock_key)"),
                            {"lock_key": WORKER_ADVISORY_LOCK_KEY},
                        )
                        logger.info("Embedded worker released singleton lock")
                    except Exception:
                        # A broken database connection releases its session lock.
                        logger.warning(
                            "Could not explicitly release worker lock; "
                            "database disconnect will release it",
                            exc_info=True,
                        )
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Embedded worker loop failed; retrying after delay")
            await _wait_or_stop(stop_event, WORKER_LOCK_RETRY_SECONDS)


async def main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_number, stop_event.set)
    await run_embedded_worker(stop_event)


if __name__ == "__main__":
    asyncio.run(main())
