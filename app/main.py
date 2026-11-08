import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from app.api.auth import router as auth_router
from app.api.business import router as business_router
from app.api.health import router as health_router
from app.api.ops import router as ops_router
from app.api.retail import router as retail_router
from app.api.telegram import router as telegram_router
from app.api.whatsapp import router as whatsapp_router
from app.core.config import get_settings
from app.db.session import engine
from app.worker import run_embedded_worker

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

WORKER_SHUTDOWN_TIMEOUT_SECONDS = 20


@asynccontextmanager
async def lifespan(_: FastAPI):
    stop_event = asyncio.Event()
    worker_task: asyncio.Task[None] | None = None

    # Tests deliberately run without a competing background consumer. Staging and
    # production run one worker inside the API process to avoid a paid Render worker.
    if settings.app_env in {"staging", "production"}:
        worker_task = asyncio.create_task(
            run_embedded_worker(stop_event),
            name="smeops-embedded-worker",
        )
        logger.info("Embedded background worker scheduled")

    try:
        yield
    finally:
        if worker_task is not None:
            stop_event.set()
            try:
                await asyncio.wait_for(
                    asyncio.shield(worker_task),
                    timeout=WORKER_SHUTDOWN_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warning(
                    "Embedded worker did not stop before shutdown deadline; cancelling"
                )
                worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await worker_task
            except asyncio.CancelledError:
                worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await worker_task
            except Exception:
                logger.exception("Embedded worker exited with an error")
        await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if settings.app_env == "production" else "/docs",
    redoc_url=None if settings.app_env == "production" else "/redoc",
    openapi_url=None if settings.app_env == "production" else "/openapi.json",
)


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=()",
    )
    response.headers.setdefault("Cache-Control", "no-store")
    if settings.app_env == "production":
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    return response


app.include_router(health_router)
app.include_router(ops_router)
app.include_router(auth_router)
app.include_router(business_router)
app.include_router(retail_router)
app.include_router(whatsapp_router)
app.include_router(telegram_router)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"service": "smeops-api", "docs": "/docs"}
