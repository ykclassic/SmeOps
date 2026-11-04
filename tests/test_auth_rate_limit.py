from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.session import AsyncSessionFactory
from app.models import AuthRateLimitBucket
from app.services.auth_rate_limit import (
    AuthRateLimitExceeded,
    _rate_limit_key,
    clear_auth_attempts,
    consume_auth_attempt,
)


@pytest.mark.asyncio
async def test_auth_attempts_are_throttled_and_identifier_is_not_stored():
    identifier = f"pilot-salon:{uuid4()}@example.com"
    async with AsyncSessionFactory() as session:
        for _ in range(5):
            await consume_auth_attempt(
                session,
                scope="login",
                identifier=identifier,
            )

    async with AsyncSessionFactory() as session:
        with pytest.raises(AuthRateLimitExceeded) as exc_info:
            await consume_auth_attempt(
                session,
                scope="login",
                identifier=identifier,
            )
        assert exc_info.value.retry_after > 0

    async with AsyncSessionFactory() as session:
        result = await session.execute(
            select(AuthRateLimitBucket).where(
                AuthRateLimitBucket.key_hash == _rate_limit_key("login", identifier)
            )
        )
        bucket = result.scalar_one()
        assert len(bucket.key_hash) == 64
        assert identifier not in bucket.key_hash
        assert bucket.attempts == 6
        assert bucket.blocked_until is not None

    async with AsyncSessionFactory() as session:
        await clear_auth_attempts(
            session,
            scope="login",
            identifier=identifier,
        )
