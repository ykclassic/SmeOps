import asyncio
from uuid import uuid4

from app.jobs.whatsapp import accept_inbound_whatsapp_job
from app.jobs.whatsapp_outbound import send_outbound_whatsapp_job
from app.jobs.worker import run_forever
from app.services.whatsapp_conversation import process_whatsapp_conversation_job


async def main() -> None:
    worker_id = f"smeops-worker-{uuid4().hex}"
    await run_forever(
        worker_id=worker_id,
        handlers={
            "whatsapp.accept_inbound": accept_inbound_whatsapp_job,
            "whatsapp.process_conversation": process_whatsapp_conversation_job,
            "whatsapp.send_text": send_outbound_whatsapp_job,
        },
    )


if __name__ == "__main__":
    asyncio.run(main())
