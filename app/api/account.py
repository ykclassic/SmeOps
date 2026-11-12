from fastapi import APIRouter, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import DbSession, require_roles
from app.core.config import get_settings
from app.core.security import verify_password
from app.models import User
from app.schemas.lifecycle import (
    OwnerDeactivationRequest,
    OwnerDeactivationResponse,
)
from app.services.tenant_lifecycle import (
    LifecycleTransitionError,
    transition_tenant_lifecycle,
)

router = APIRouter(prefix="/account", tags=["account management"])
OwnerUser = require_roles("owner")


@router.post(
    "/deactivate",
    response_model=OwnerDeactivationResponse,
    status_code=status.HTTP_200_OK,
)
async def deactivate_owner_account(
    payload: OwnerDeactivationRequest,
    session: DbSession,
    user: User = __import__("fastapi").Depends(OwnerUser),
) -> OwnerDeactivationResponse:
    # Keep this API unavailable until all ingress, worker, outbound and
    # restricted-reactivation enforcement work is complete and accepted.
    if not get_settings().owner_deactivation_api_enabled:
        raise HTTPException(status_code=404, detail="Not found")

    if not verify_password(payload.password.get_secret_value(), user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Recent password verification failed",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        result = await transition_tenant_lifecycle(
            session,
            tenant_id=user.tenant_id,
            actor_user_id=user.id,
            target_status="deactivated",
            reason=payload.reason,
        )
    except LifecycleTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        # Authentication has already opened the caller's AsyncSession
        # transaction. The lifecycle service uses a SAVEPOINT and deliberately
        # leaves this outer transaction under endpoint ownership.
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Account state could not be saved",
        ) from exc

    return OwnerDeactivationResponse(
        status="deactivated",
        changed=result.changed,
        tenant_id=result.tenant_id,
    )
