"""Dataset request endpoints.

Scoping rule, applied in one place (`_visible_requests`): a client sees only
their own requests, an operator or admin sees all. It is applied as a WHERE
clause on every query rather than as a check after loading, so a client cannot
fetch another client's request by guessing its id.
"""

import logging

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.deps import get_current_user, require_client, require_operator
from app.errors import NotFound
from app.models import (
    Assignment,
    Episode,
    Request,
    RequestStatus,
    RequestStatusEvent,
    Role,
    User,
)
from app.schemas import (
    AssignEpisodes,
    EpisodeOut,
    RequestCreate,
    RequestDetail,
    RequestOut,
    RequestPage,
    StatusChange,
    StatusEventOut,
)
from app.services import assignments as assignment_service
from app.services import transitions as transition_service

logger = logging.getLogger("app.requests")

router = APIRouter(prefix="/api/requests", tags=["requests"])


def _assigned_count_column():
    """Correlated subquery so list endpoints get their counts in one query
    instead of one extra query per request row."""
    return (
        select(func.count(Assignment.id))
        .where(Assignment.request_id == Request.id)
        .correlate(Request)
        .scalar_subquery()
    )


def _visible_requests(user: User) -> Select:
    statement = select(Request, _assigned_count_column().label("assigned_count"))
    if user.role is Role.client:
        statement = statement.where(Request.client_id == user.id)
    return statement


def _to_out(request: Request, assigned_count: int, user: User) -> RequestOut:
    return RequestOut(
        id=request.id,
        task_name=request.task_name,
        episodes_requested=request.episodes_requested,
        deadline=request.deadline,
        notes=request.notes,
        status=request.status,
        created_at=request.created_at,
        updated_at=request.updated_at,
        client=request.client,
        assigned_count=assigned_count,
        allowed_transitions=transition_service.permitted_next_statuses(
            request.status, user.role
        ),
    )


def _load_visible_request(db: Session, request_id: int, user: User) -> tuple[Request, int]:
    row = db.execute(
        _visible_requests(user)
        .where(Request.id == request_id)
        .options(selectinload(Request.client))
    ).first()
    if row is None:
        # Deliberately 404 and not 403: telling a client that request 7 exists
        # but belongs to someone else is itself a leak.
        raise NotFound(f"request {request_id} not found")
    return row[0], row[1]


@router.post("", response_model=RequestOut, status_code=status.HTTP_201_CREATED)
def create_request(
    payload: RequestCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_client),
) -> RequestOut:
    """Clients only. A request always starts at `submitted`."""
    request = Request(
        client_id=user.id,
        task_name=payload.task_name.strip(),
        episodes_requested=payload.episodes_requested,
        deadline=payload.deadline,
        notes=payload.notes,
        status=RequestStatus.submitted,
    )
    db.add(request)
    db.flush()

    # The opening row of the audit trail, so "when was this submitted" is
    # answered by the same table as every later change.
    db.add(
        RequestStatusEvent(
            request_id=request.id,
            from_status=None,
            to_status=RequestStatus.submitted,
            actor_id=user.id,
        )
    )
    db.commit()
    db.refresh(request)

    logger.info(
        "request_created", extra={"request_id": request.id, "client_id": user.id}
    )
    return _to_out(request, assigned_count=0, user=user)


@router.get("", response_model=RequestPage)
def list_requests(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    status_filter: RequestStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> RequestPage:
    statement = _visible_requests(user).options(selectinload(Request.client))
    if status_filter is not None:
        statement = statement.where(Request.status == status_filter)

    total = db.scalar(
        select(func.count()).select_from(statement.order_by(None).subquery())
    )

    rows = db.execute(
        statement.order_by(Request.created_at.desc(), Request.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    return RequestPage(
        items=[_to_out(row[0], row[1], user) for row in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.get("/{request_id}", response_model=RequestDetail)
def get_request(
    request_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RequestDetail:
    request, assigned_count = _load_visible_request(db, request_id, user)

    episodes = db.scalars(
        select(Episode)
        .join(Assignment, Assignment.episode_id == Episode.episode_id)
        .where(Assignment.request_id == request.id)
        .order_by(Episode.recorded_at)
    ).all()

    history = db.execute(
        select(RequestStatusEvent, User.name)
        .join(User, User.id == RequestStatusEvent.actor_id)
        .where(RequestStatusEvent.request_id == request.id)
        .order_by(RequestStatusEvent.created_at, RequestStatusEvent.id)
    ).all()

    base = _to_out(request, assigned_count, user)
    return RequestDetail(
        **base.model_dump(),
        episodes=[EpisodeOut.model_validate(e) for e in episodes],
        history=[
            StatusEventOut(
                from_status=event.from_status,
                to_status=event.to_status,
                actor_id=event.actor_id,
                actor_name=actor_name,
                note=event.note,
                created_at=event.created_at,
            )
            for event, actor_name in history
        ],
    )


@router.post("/{request_id}/status", response_model=RequestOut)
def change_status(
    request_id: int,
    payload: StatusChange,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> RequestOut:
    """Move a request through the workflow.

    Role checks live in the transition service, because which roles may make a
    move depends on which move it is: operators advance the work, clients
    accept or reject it.
    """
    request, _ = _load_visible_request(db, request_id, user)

    transition_service.apply_transition(
        db, request=request, to_status=payload.to_status, actor=user, note=payload.note
    )
    db.commit()
    db.refresh(request)

    assigned_count = transition_service.assigned_episode_count(db, request.id)
    logger.info(
        "request_status_changed",
        extra={
            "request_id": request.id,
            "to_status": request.status.value,
            "actor_id": user.id,
        },
    )
    return _to_out(request, assigned_count, user)


@router.post("/{request_id}/assignments", response_model=RequestOut)
def assign_episodes(
    request_id: int,
    payload: AssignEpisodes,
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> RequestOut:
    request, _ = _load_visible_request(db, request_id, user)

    created = assignment_service.assign_episodes(
        db, request=request, episode_ids=payload.episode_ids, actor=user
    )
    db.commit()
    db.refresh(request)

    logger.info(
        "episodes_assigned",
        extra={
            "request_id": request.id,
            "count": len(created),
            "actor_id": user.id,
        },
    )
    assigned_count = transition_service.assigned_episode_count(db, request.id)
    return _to_out(request, assigned_count, user)


@router.delete(
    "/{request_id}/assignments/{episode_id}", status_code=status.HTTP_204_NO_CONTENT
)
def unassign_episode(
    request_id: int,
    episode_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> Response:
    request, _ = _load_visible_request(db, request_id, user)
    assignment_service.unassign_episode(db, request=request, episode_id=episode_id)
    db.commit()

    logger.info(
        "episode_unassigned",
        extra={
            "request_id": request.id,
            "episode_id": episode_id,
            "actor_id": user.id,
        },
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
