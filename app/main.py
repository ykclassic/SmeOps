import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.auth import router as auth_router
from app.api.business import router as business_router
from app.api.health import router as health_router
from app.api.retail import router as retail_router
from app.api.whatsapp import router as whatsapp_router
from app.core.config import get_settings
from app.db.session import engine

settings = get_settings()
logging.basicConfig(level=settings.log_level)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(business_router)
app.include_router(retail_router)
app.include_router(whatsapp_router)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"service": "smeops-api", "docs": "/docs"}
