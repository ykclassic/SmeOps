"""Owner-initiated tenant lifecycle transitions.

Transaction contract:
- The caller owns the outer AsyncSession transaction and must commit or roll it
  back (normally with an explicit session.begin() context).
- This service uses a SAVEPOINT, flushes its own writes, and never commits or
  rolls back the caller's transaction.
- Any exception rolls back this service's work to the SAVEPOINT. The caller may
  catch the exception without leaving a partial tenant/audit mutation behind.

Lock contract:
- The tenant row is the lifecycle serialization mutex and is always locked
  first. Existing REST booking/retail mutations and salon booking use the same
  tenant-row SELECT FOR UPDATE lock before child-row work.
- The actor is validated after acquiring the tenant lock, but is not separately
  locked: actor-row locking is not part of the shared mutation protocol and
  would add an unnecessary lock edge/deadlock risk.
- The tenant lock remains held until the caller commits/rolls back the outer
  transaction, not merely until this service releases its SAVEPOINT.
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
    """Apply an owner lifecycle transition inside a caller-owned transaction.

    The SAVEPOINT makes the service's tenant and audit writes atomic even when
    the caller catches a service exception. Successful changes remain
    uncommitted until the caller commits the outer transaction.
    """
    if target_status not in {"active", "deactivated"}:
        raise LifecycleTransitionError("Unsupported lifecycle target state.")
    normalized_reason = _validate_reason(reason)
    if not session.in_transaction():
        raise LifecycleTransitionError(
            "The caller must start the outer database transaction before this operation."
        )

    async with session.begin_nested():
        tenant_result = await session.execute(
            select(Tenant).where(Tenant.id == tenant_id).with_for_update()
        )
        tenant = tenant_result.scalar_one_or_none()
        if tenant is None:
            raise LifecycleTransitionError("Business account was not found.")

        # Lock order is tenant first, then ordinary reads. Do not add a User
        # FOR UPDATE lock here: booking/retail writers do not share that lock.
        actor_result = await session.execute(
            select(User).where(
                User.id == actor_user_id,
                User.tenant_id == tenant_id,
                User.status == "active",
                User.role == "owner",
            )
        )
        actor = actor_result.scalar_one_or_none()
        if actor is None:
            raise LifecycleTransitionError("An active owner of this business is required.")

        previous_status = tenant.status
        if previous_status == target_status:
            result = LifecycleTransitionResult(
                tenant_id=tenant.id,
                previous_status=previous_status,
                status=target_status,
                changed=False,
                audit_event_id=None,
            )
        else:
            allowed_transition = (
                (previous_status == "active" and target_status == "deactivated")
                or (previous_status == "deactivated" and target_status == "active")
            )
            if not allowed_transition:
                raise LifecycleTransitionError(
                    f"Transition from {previous_status!r} to {target_status!r} "
                    "is not permitted."
                )

            tenant.status = target_status
            event_id = uuid4()
            session.add(
                AuditEvent(
                    id=event_id,
                    tenant_id=tenant.id,
                    actor_user_id=actor.id,
                    event_type=(
                        "tenant.deactivated"
                        if target_status == "deactivated"
                        else "tenant.reactivated"
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
            # Surface constraint/insert failures while still inside the
            # SAVEPOINT so the tenant update is rolled back with the audit.
            await session.flush()
            result = LifecycleTransitionResult(
                tenant_id=tenant.id,
                previous_status=previous_status,
                status=target_status,
                changed=True,
                audit_event_id=event_id,
            )

    return result
