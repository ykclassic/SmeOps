from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_access_token
from app.db.session import get_db_session
from app.models import Tenant, User

bearer_scheme = HTTPBearer(auto_error=False)
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _decode_identity(
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    try:
        return decode_access_token(credentials.credentials)
    except (jwt.InvalidTokenError, ValueError, TypeError):
        raise _unauthorized() from None


async def get_current_user(
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    user_id, tenant_id = await _decode_identity(credentials)
    result = await session.execute(
        select(User)
        .join(Tenant, Tenant.id == User.tenant_id)
        .where(
            User.id == user_id,
            User.tenant_id == tenant_id,
            User.status == "active",
            Tenant.status == "active",
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise _unauthorized()
    return user


async def get_lifecycle_owner(
    session: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    """Authenticate an owner for lifecycle endpoints only.

    This intentionally accepts active or deactivated tenants, but never
    suspended/pending tenants, disabled users, or non-owner roles. It does not
    replace CurrentUser and must not be used by ordinary business routes.
    """
    user_id, tenant_id = await _decode_identity(credentials)
    result = await session.execute(
        select(User)
        .join(Tenant, Tenant.id == User.tenant_id)
        .where(
            User.id == user_id,
            User.tenant_id == tenant_id,
            User.status == "active",
            User.role == "owner",
            Tenant.status.in_(("active", "deactivated")),
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise _unauthorized()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
LifecycleOwner = Annotated[User, Depends(get_lifecycle_owner)]


def require_roles(*allowed_roles: str):
    async def role_dependency(user: CurrentUser) -> User:
        if user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return user

    return role_dependency
