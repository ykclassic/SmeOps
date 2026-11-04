import hashlib
import hmac
import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, delete, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import AuthRateLimitBucket

AUTH_WINDOW = timedelta(minutes=15)
AUTH_MAX_ATTEMPTS = 5


class AuthRateLimitExceeded(RuntimeError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("Authentication rate limit exceeded")
        self.retry_after = max(1, retry_after)


def _rate_limit_key(scope: str, identifier: str) -> str:
    secret = get_settings().app_secret_key.get_secret_value().encode()
    message = f"{scope}:{identifier.strip().casefold()}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


async def consume_auth_attempt(
    session: AsyncSession,
    *,
    scope: str,
    identifier: str,
) -> None:
    if not identifier.strip():
        raise ValueError("Rate-limit identifier must not be empty")
    now = datetime.now(UTC)
    key_hash = _rate_limit_key(scope, identifier)
    bucket = AuthRateLimitBucket
    currently_blocked = bucket.blocked_until.is_not(None) & (bucket.blocked_until > now)
    window_expired = bucket.window_started_at <= now - AUTH_WINDOW

    statement = (
        insert(bucket)
        .values(
            key_hash=key_hash,
            window_started_at=now,
            attempts=1,
            blocked_until=None,
        )
        .on_conflict_do_update(
            index_elements=[bucket.key_hash],
            set_={
                "attempts": case(
                    (currently_blocked, bucket.attempts),
                    (window_expired, 1),
                    else_=bucket.attempts + 1,
                ),
                "window_started_at": case(
                    (currently_blocked, bucket.window_started_at),
                    (window_expired, now),
                    else_=bucket.window_started_at,
                ),
                "blocked_until": case(
                    (currently_blocked, bucket.blocked_until),
                    (window_expired, None),
                    else_=bucket.blocked_until,
                ),
                "updated_at": now,
            },
        )
        .returning(bucket.attempts, bucket.blocked_until)
    )
    result = await session.execute(statement)
    attempts, blocked_until = result.one()

    if blocked_until is not None and blocked_until > now:
        retry_after = math.ceil((blocked_until - now).total_seconds())
        await session.commit()
        raise AuthRateLimitExceeded(retry_after)

    if attempts > AUTH_MAX_ATTEMPTS:
        blocked_until = now + AUTH_WINDOW
        await session.execute(
            update(bucket)
            .where(bucket.key_hash == key_hash, bucket.blocked_until.is_(None))
            .values(blocked_until=blocked_until, updated_at=now)
        )
        await session.commit()
        raise AuthRateLimitExceeded(math.ceil(AUTH_WINDOW.total_seconds()))

    await session.execute(
        delete(bucket).where(bucket.updated_at < now - timedelta(days=1))
    )
    await session.commit()


async def clear_auth_attempts(
    session: AsyncSession,
    *,
    scope: str,
    identifier: str,
) -> None:
    key_hash = _rate_limit_key(scope, identifier)
    await session.execute(
        delete(AuthRateLimitBucket).where(AuthRateLimitBucket.key_hash == key_hash)
    )
    await session.commit()
