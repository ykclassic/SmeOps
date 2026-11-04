import asyncio
from uuid import uuid4

from app.jobs.whatsapp import accept_inbound_whatsapp_job
from app.jobs.worker import run_forever


async def main() -> None:
    worker_id = f"smeops-worker-{uuid4().hex}"
    await run_forever(
        worker_id=worker_id,
        handlers={"whatsapp.accept_inbound": accept_inbound_whatsapp_job},
    )


if __name__ == "__main__":
    asyncio.run(main())
