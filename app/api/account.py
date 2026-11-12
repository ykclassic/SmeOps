from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import DbSession, require_roles
from app.core.config import get_settings
from app.core.security import verify_password
from app.models import User
from app.schemas.lifecycle import (
    OwnerDeactivationRequest,
    OwnerDeactivationResponse,
)
from app.services.auth_rate_limit import (
    AuthRateLimitExceeded,
    clear_auth_attempts,
    consume_auth_attempt,
)
from app.services.tenant_lifecycle import (
    LifecycleTransitionError,
    transition_tenant_lifecycle,
)

router = APIRouter(prefix="/account", tags=["account management"])
OwnerUser = Annotated[User, Depends(require_roles("owner"))]


@router.post(
    "/deactivate",
    response_model=OwnerDeactivationResponse,
    status_code=status.HTTP_200_OK,
)
async def deactivate_owner_account(
    payload: OwnerDeactivationRequest,
    session: DbSession,
    user: OwnerUser,
) -> OwnerDeactivationResponse:
    # Keep this API unavailable until all ingress, worker, outbound and
    # restricted-reactivation enforcement work is complete.
    if not get_settings().owner_deactivation_api_enabled:
        raise HTTPException(status_code=404, detail="Not found")

    # Persist failed step-up attempts independently from the lifecycle
    # transaction. The shared limiter caps attempts at five per 15-minute window.
    try:
        await consume_auth_attempt(
            session,
            scope="owner-deactivation",
            identifier=str(user.id),
        )
    except AuthRateLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many reauthentication attempts. Try again later.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc

    # The authenticated User instance may have been loaded before a concurrent
    # password change. Read the hash directly from the database after limiter
    # commits so step-up verification never trusts a stale identity-map value.
    current_password_hash = await session.scalar(
        select(User.password_hash).where(
            User.id == user.id,
            User.tenant_id == user.tenant_id,
            User.status == "active",
        )
    )
    if current_password_hash is None or not verify_password(
        payload.password.get_secret_value(),
        current_password_hash,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Recent password verification failed",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Clear failed attempts after successful step-up verification. The limiter
    # helper commits its own small transaction, so begin the lifecycle
    # transaction only after it returns.
    await clear_auth_attempts(
        session,
        scope="owner-deactivation",
        identifier=str(user.id),
    )

    try:
        async with session.begin():
            result = await transition_tenant_lifecycle(
                session,
                tenant_id=user.tenant_id,
                actor_user_id=user.id,
                target_status="deactivated",
                reason=payload.reason,
            )
    except LifecycleTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account state could not be saved",
        ) from exc

    return OwnerDeactivationResponse(
        status="deactivated",
        changed=result.changed,
        tenant_id=result.tenant_id,
    )
