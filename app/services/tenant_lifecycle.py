"""Centralized, transactional tenant lifecycle transitions.

Only owner-initiated voluntary deactivation/reactivation is supported here.
Platform-admin suspension and reactivation require the separate platform-admin
identity and audit boundary and are deliberately not exposed by this service.
"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, Tenant, User

LifecycleTarget = Literal["active", "deactivated"]
MAX_REASON_LENGTH = 500


class LifecycleTransitionError(ValueError):
    """A lifecycle transition was not authorized or is not valid."""


@dataclass(frozen=True, slots=True)
class LifecycleTransitionResult:
    tenant_id: UUID
    previous_status: str
    status: LifecycleTarget
    changed: bool
    audit_event_id: UUID | None


def _validate_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    if not isinstance(reason, str):
        raise LifecycleTransitionError("Reason must be text.")
    normalized = reason.strip()
    if len(normalized) > MAX_REASON_LENGTH:
        raise LifecycleTransitionError(
            f"Reason must be {MAX_REASON_LENGTH} characters or fewer."
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise LifecycleTransitionError("Reason must not contain control characters.")
    return normalized or None


async def transition_tenant_lifecycle(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    actor_user_id: UUID,
    target_status: LifecycleTarget,
    reason: str | None = None,
) -> LifecycleTransitionResult:
    """Apply one owner lifecycle transition and commit its audit event atomically.

    The tenant row is locked first. Booking and retail mutation paths already use
    this same tenant-row lock, so a successful deactivation serializes against
    those transactions. Callers must supply actor identity from server-side
    authentication; this function independently reloads and validates it.

    This function owns the transaction commit. A no-op retry commits no audit
    event, making repeated requests idempotent.
    """
    if target_status not in {"active", "deactivated"}:
        raise LifecycleTransitionError("Unsupported lifecycle target state.")
    normalized_reason = _validate_reason(reason)

    tenant_result = await session.execute(
        select(Tenant).where(Tenant.id == tenant_id).with_for_update()
    )
    tenant = tenant_result.scalar_one_or_none()
    if tenant is None:
        raise LifecycleTransitionError("Business account was not found.")

    actor_result = await session.execute(
        select(User)
        .where(
            User.id == actor_user_id,
            User.tenant_id == tenant_id,
            User.status == "active",
            User.role == "owner",
        )
        .with_for_update()
    )
    actor = actor_result.scalar_one_or_none()
    if actor is None:
        raise LifecycleTransitionError("An active owner of this business is required.")

    previous_status = tenant.status
    if previous_status == target_status:
        await session.commit()
        return LifecycleTransitionResult(
            tenant_id=tenant.id,
            previous_status=previous_status,
            status=target_status,
            changed=False,
            audit_event_id=None,
        )

    allowed_transition = (
        (previous_status == "active" and target_status == "deactivated")
        or (previous_status == "deactivated" and target_status == "active")
    )
    if not allowed_transition:
        raise LifecycleTransitionError(
            f"Transition from {previous_status!r} to {target_status!r} is not permitted."
        )

    tenant.status = target_status
    event_id = uuid4()
    session.add(
        AuditEvent(
            id=event_id,
            tenant_id=tenant.id,
            actor_user_id=actor.id,
            event_type=(
                "tenant.deactivated" if target_status == "deactivated" else "tenant.reactivated"
            ),
            subject_type="tenant",
            subject_id=tenant.id,
            payload={
                "from_status": previous_status,
                "to_status": target_status,
                "reason": normalized_reason,
            },
        )
    )

    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise

    return LifecycleTransitionResult(
        tenant_id=tenant.id,
        previous_status=previous_status,
        status=target_status,
        changed=True,
        audit_event_id=event_id,
    )
