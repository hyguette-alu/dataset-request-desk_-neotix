"""The request status workflow.

    submitted -> in_progress -> delivered -> accepted
                                         \\-> rejected -> in_progress (rework)

Two separate rules govern a change and both are checked here:

1. The edge must exist. Anything not in `TRANSITIONS` is refused, including
   skipping a step (submitted -> delivered) and moving on from the terminal
   `accepted` state.
2. The caller's role must own that edge. Clients accept or reject; operators
   and admins do everything else. A client may only act on their own request.

Every accepted change appends a row to request_status_events, so the history
of who moved what and when is a queryable table rather than a log file.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import InvalidTransition, PermissionDenied
from app.models import (
    Assignment,
    Request,
    RequestStatus,
    RequestStatusEvent,
    Role,
    User,
)

OPERATOR_ROLES = frozenset({Role.operator, Role.admin})
CLIENT_ROLES = frozenset({Role.client})

# from_status -> to_status -> roles permitted to make that move
TRANSITIONS: dict[RequestStatus, dict[RequestStatus, frozenset[Role]]] = {
    RequestStatus.submitted: {
        RequestStatus.in_progress: OPERATOR_ROLES,
    },
    RequestStatus.in_progress: {
        RequestStatus.delivered: OPERATOR_ROLES,
    },
    RequestStatus.delivered: {
        RequestStatus.accepted: CLIENT_ROLES,
        RequestStatus.rejected: CLIENT_ROLES,
    },
    RequestStatus.rejected: {
        RequestStatus.in_progress: OPERATOR_ROLES,
    },
    # Terminal: an accepted delivery is final.
    RequestStatus.accepted: {},
}

# Statuses during which an operator may still attach or detach episodes.
# Once delivered, the set of episodes is what the client is reviewing, so it
# is frozen until they reject it and it goes back to in_progress.
ASSIGNABLE_STATUSES = frozenset(
    {RequestStatus.submitted, RequestStatus.in_progress, RequestStatus.rejected}
)


def allowed_transitions(status: RequestStatus) -> dict[RequestStatus, frozenset[Role]]:
    return TRANSITIONS.get(status, {})


def permitted_next_statuses(status: RequestStatus, role: Role) -> list[RequestStatus]:
    """What this role could do next. Used to drive the UI; the API re-checks."""
    return [
        target
        for target, roles in allowed_transitions(status).items()
        if role in roles
    ]


def assigned_episode_count(db: Session, request_id: int) -> int:
    return db.scalar(
        select(func.count())
        .select_from(Assignment)
        .where(Assignment.request_id == request_id)
    ) or 0


def apply_transition(
    db: Session,
    request: Request,
    to_status: RequestStatus,
    actor: User,
    note: str | None = None,
) -> Request:
    """Validate and perform a status change, recording who did it."""
    current = request.status

    edges = allowed_transitions(current)
    if to_status not in edges:
        raise InvalidTransition(
            f"cannot move a request from {current.value} to {to_status.value}",
            from_status=current.value,
            to_status=to_status.value,
            allowed=[s.value for s in edges],
        )

    if actor.role not in edges[to_status]:
        raise PermissionDenied(
            f"role {actor.role.value} may not move a request "
            f"from {current.value} to {to_status.value}"
        )

    # A client may only act on their own request. Operators and admins see all.
    if actor.role is Role.client and request.client_id != actor.id:
        raise PermissionDenied("this request belongs to another client")

    if to_status is RequestStatus.delivered:
        assigned = assigned_episode_count(db, request.id)
        if assigned < request.episodes_requested:
            raise InvalidTransition(
                f"request needs {request.episodes_requested} episodes assigned "
                f"before it can be delivered, but has {assigned}",
                assigned=assigned,
                required=request.episodes_requested,
            )

    request.status = to_status
    db.add(
        RequestStatusEvent(
            request_id=request.id,
            from_status=current,
            to_status=to_status,
            actor_id=actor.id,
            note=note,
        )
    )
    db.flush()
    return request
