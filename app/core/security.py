from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from pwdlib import PasswordHash

from app.core.config import get_settings

password_hasher = PasswordHash.recommended()
ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return password_hasher.verify(password, password_hash)


def create_access_token(*, user_id: UUID, tenant_id: UUID) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "iss": settings.jwt_issuer,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
    }
    return jwt.encode(
        payload,
        settings.app_secret_key.get_secret_value(),
        algorithm=ALGORITHM,
    )


def decode_access_token(token: str) -> tuple[UUID, UUID]:
    settings = get_settings()
    payload = jwt.decode(
        token,
        settings.app_secret_key.get_secret_value(),
        algorithms=[ALGORITHM],
        issuer=settings.jwt_issuer,
        options={"require": ["exp", "iat", "iss", "sub", "tenant_id"]},
    )
    return UUID(payload["sub"]), UUID(payload["tenant_id"])
