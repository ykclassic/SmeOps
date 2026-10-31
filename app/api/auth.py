from uuid import uuid4

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import CurrentUser, DbSession
from app.core.security import create_access_token, hash_password, verify_password
from app.models import Tenant, User
from app.schemas.auth import (
    CurrentUserResponse,
    LoginRequest,
    RegisterRequest,
    TokenResponse,
)

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, session: DbSession) -> TokenResponse:
    existing = await session.execute(select(Tenant.id).where(Tenant.slug == payload.tenant_slug))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Business identifier is unavailable")

    tenant = Tenant(
        id=uuid4(),
        name=payload.business_name,
        slug=payload.tenant_slug,
        business_timezone=payload.business_timezone,
        status="active",
    )
    user = User(
        id=uuid4(),
        tenant_id=tenant.id,
        email=str(payload.email).strip().lower(),
        password_hash=hash_password(payload.password),
        role="owner",
        status="active",
    )
    session.add_all([tenant, user])
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Business identifier or account already exists",
        ) from exc

    token = create_access_token(user_id=user.id, tenant_id=tenant.id)
    return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, session: DbSession) -> TokenResponse:
    result = await session.execute(
        select(User)
        .join(Tenant, Tenant.id == User.tenant_id)
        .where(
            Tenant.slug == payload.tenant_slug,
            Tenant.status == "active",
            User.email == str(payload.email).strip().lower(),
            User.status == "active",
        )
    )
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid business identifier, email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = create_access_token(user_id=user.id, tenant_id=user.tenant_id)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=CurrentUserResponse)
async def me(user: CurrentUser) -> CurrentUserResponse:
    return CurrentUserResponse(
        id=str(user.id),
        tenant_id=str(user.tenant_id),
        email=user.email,
        role=user.role,
    )
