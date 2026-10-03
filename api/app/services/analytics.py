"""Analytics queries.

Every figure below is computed by Postgres and comes back as a handful of
aggregate rows. Nothing here loads episodes into Python to count them, which
is the difference between a query that stays flat as the table grows and one
that falls over. See NOTES.md for how each behaves at 5 million episodes.

Date range conventions, because the brief leaves them open:

* `date_from` and `date_to` are both inclusive calendar days, interpreted in
  UTC. Internally that becomes [date_from 00:00, date_to+1 day 00:00).
* Episode metrics filter on `recorded_at` (when the robot ran).
* Request counts filter on `requests.created_at` (when the client asked).
* The median filters on when the request was *delivered*, so the number means
  "requests delivered in this window took this long", not "requests created in
  this window", which could not be answered until they were all finished.
"""

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import Date, Float, cast, func, select
from sqlalchemy.orm import Session

from app.models import Episode, Quality, Request, RequestStatus, RequestStatusEvent

TOP_TASK_LIMIT = 5


def _window(date_from: date, date_to: date) -> tuple[datetime, datetime]:
    start = datetime.combine(date_from, time.min, tzinfo=timezone.utc)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return start, end


def episodes_per_day_per_robot(
    db: Session, date_from: date, date_to: date
) -> list[dict]:
    """GROUP BY day, robot. Uses ix_episodes_robot_recorded_at."""
    start, end = _window(date_from, date_to)

    day = cast(func.timezone("UTC", Episode.recorded_at), Date).label("day")
    statement = (
        select(day, Episode.robot_id, func.count().label("episodes"))
        .where(Episode.recorded_at >= start, Episode.recorded_at < end)
        .group_by(day, Episode.robot_id)
        .order_by(day, Episode.robot_id)
    )

    return [
        {"day": row.day, "robot_id": row.robot_id, "episodes": row.episodes}
        for row in db.execute(statement)
    ]


def requests_by_status(db: Session, date_from: date, date_to: date) -> list[dict]:
    start, end = _window(date_from, date_to)

    statement = (
        select(Request.status, func.count().label("count"))
        .where(Request.created_at >= start, Request.created_at < end)
        .group_by(Request.status)
        .order_by(Request.status)
    )
    counts = {row.status: row.count for row in db.execute(statement)}

    # Report every status, including the ones with no requests, so a caller
    # does not have to guess whether a missing key means zero.
    return [
        {"status": status, "count": counts.get(status, 0)} for status in RequestStatus
    ]


def median_submitted_to_delivered_hours(
    db: Session, date_from: date, date_to: date
) -> float | None:
    """Median wall-clock hours from submission to first delivery.

    Computed from the status-event history with percentile_cont, which
    interpolates between the two middle values for an even count. A request
    that was rejected and delivered again counts its *first* delivery: that is
    when operations finished the work the client originally asked for.
    """
    start, end = _window(date_from, date_to)

    milestones = (
        select(
            RequestStatusEvent.request_id.label("request_id"),
            func.min(RequestStatusEvent.created_at)
            .filter(RequestStatusEvent.to_status == RequestStatus.submitted)
            .label("submitted_at"),
            func.min(RequestStatusEvent.created_at)
            .filter(RequestStatusEvent.to_status == RequestStatus.delivered)
            .label("delivered_at"),
        )
        .group_by(RequestStatusEvent.request_id)
        .subquery()
    )

    elapsed_hours = cast(
        func.extract("epoch", milestones.c.delivered_at - milestones.c.submitted_at),
        Float,
    ) / 3600.0

    statement = select(
        func.percentile_cont(0.5).within_group(elapsed_hours)
    ).where(
        milestones.c.submitted_at.is_not(None),
        milestones.c.delivered_at.is_not(None),
        milestones.c.delivered_at >= start,
        milestones.c.delivered_at < end,
    )

    median = db.scalar(statement)
    return round(float(median), 2) if median is not None else None


def top_tasks_by_good_episodes(
    db: Session, date_from: date, date_to: date, limit: int = TOP_TASK_LIMIT
) -> list[dict]:
    """Served by the partial index on (task_name, recorded_at) WHERE
    quality = 'good', so the bad and usable rows are never visited."""
    start, end = _window(date_from, date_to)

    count = func.count().label("good_episodes")
    statement = (
        select(Episode.task_name, count)
        .where(
            Episode.quality == Quality.good,
            Episode.recorded_at >= start,
            Episode.recorded_at < end,
        )
        .group_by(Episode.task_name)
        # Secondary sort on name so equal counts come back in a stable order
        # instead of whatever the planner happens to produce.
        .order_by(count.desc(), Episode.task_name.asc())
        .limit(limit)
    )

    return [
        {"task_name": row.task_name, "good_episodes": row.good_episodes}
        for row in db.execute(statement)
    ]


def build_report(db: Session, date_from: date, date_to: date) -> dict:
    return {
        "date_from": date_from,
        "date_to": date_to,
        "episodes_per_day_per_robot": episodes_per_day_per_robot(
            db, date_from, date_to
        ),
        "requests_by_status": requests_by_status(db, date_from, date_to),
        "median_submitted_to_delivered_hours": median_submitted_to_delivered_hours(
            db, date_from, date_to
        ),
        "top_tasks_by_good_episodes": top_tasks_by_good_episodes(
            db, date_from, date_to
        ),
    }
