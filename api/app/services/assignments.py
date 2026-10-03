"""Attaching episodes to requests.

Rules enforced here:

* only `good` or `usable` episodes may be assigned (never `bad`);
* an episode belongs to at most one request at a time;
* episodes can only be attached while the request is still being worked on.

The second rule is also a UNIQUE constraint on assignments.episode_id. The
check below exists to return a useful 409 instead of an integrity error, but
the constraint is what actually makes it true: two operators assigning the
same episode at the same moment would both pass a Python check, and the
database is what stops the second one.
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import AssignmentRejected, NotFound
from app.models import ASSIGNABLE_QUALITIES, Assignment, Episode, Request, User
from app.services.transitions import ASSIGNABLE_STATUSES


def assign_episodes(
    db: Session, request: Request, episode_ids: list[str], actor: User
) -> list[Assignment]:
    """Attach episodes to a request. All or nothing: if any episode fails a
    rule, none are attached."""
    if request.status not in ASSIGNABLE_STATUSES:
        raise AssignmentRejected(
            f"episodes cannot be assigned while the request is {request.status.value}",
            status=request.status.value,
        )

    wanted = list(dict.fromkeys(episode_id.strip().upper() for episode_id in episode_ids))
    if not wanted:
        raise AssignmentRejected("no episodes given")

    episodes = {
        episode.episode_id: episode
        for episode in db.scalars(
            select(Episode).where(Episode.episode_id.in_(wanted))
        )
    }

    missing = [episode_id for episode_id in wanted if episode_id not in episodes]
    if missing:
        raise NotFound(f"unknown episodes: {', '.join(sorted(missing))}", missing=missing)

    unassignable = [
        episode_id
        for episode_id in wanted
        if episodes[episode_id].quality not in ASSIGNABLE_QUALITIES
    ]
    if unassignable:
        raise AssignmentRejected(
            "only good or usable episodes can be assigned; rejected: "
            + ", ".join(sorted(unassignable)),
            episodes=unassignable,
        )

    already_taken = {
        assignment.episode_id: assignment.request_id
        for assignment in db.scalars(
            select(Assignment).where(Assignment.episode_id.in_(wanted))
        )
    }
    if already_taken:
        detail = ", ".join(
            f"{episode_id} (request {request_id})"
            for episode_id, request_id in sorted(already_taken.items())
        )
        raise AssignmentRejected(
            f"already assigned to another request: {detail}",
            episodes=sorted(already_taken),
        )

    created = [
        Assignment(request_id=request.id, episode_id=episode_id, assigned_by=actor.id)
        for episode_id in wanted
    ]
    db.add_all(created)

    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        # Lost the race against a concurrent assignment; the UNIQUE constraint
        # on assignments.episode_id is what caught it.
        raise AssignmentRejected(
            "one of these episodes was assigned to another request concurrently"
        ) from exc

    return created


def unassign_episode(db: Session, request: Request, episode_id: str) -> None:
    if request.status not in ASSIGNABLE_STATUSES:
        raise AssignmentRejected(
            f"episodes cannot be removed while the request is {request.status.value}",
            status=request.status.value,
        )

    normalised = episode_id.strip().upper()
    assignment = db.scalar(
        select(Assignment).where(
            Assignment.request_id == request.id,
            Assignment.episode_id == normalised,
        )
    )
    if assignment is None:
        raise NotFound(f"{normalised} is not assigned to this request")

    db.delete(assignment)
    db.flush()
