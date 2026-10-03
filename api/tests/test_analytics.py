"""Analytics aggregates.

These assert on numbers, not on SQL. The point of the tests is that the
grouping, the date-window boundaries and the median are right; the point of
doing it in SQL is covered in NOTES.md.
"""

from datetime import date, datetime, timedelta, timezone

from app.models import Quality, RequestStatus, RequestStatusEvent
from app.services.analytics import (
    episodes_per_day_per_robot,
    median_submitted_to_delivered_hours,
    requests_by_status,
    top_tasks_by_good_episodes,
)
from tests.conftest import auth_headers, make_episode, make_request

AUG = date(2026, 8, 1)
SEP = date(2026, 8, 31)


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 8, day, hour, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Episodes per day, per robot
# --------------------------------------------------------------------------


def test_episodes_are_grouped_by_day_and_robot(db):
    make_episode(db, "EP-1", robot_id="arm-01", recorded_at=at(10, 9))
    make_episode(db, "EP-2", robot_id="arm-01", recorded_at=at(10, 23))
    make_episode(db, "EP-3", robot_id="arm-02", recorded_at=at(10))
    make_episode(db, "EP-4", robot_id="arm-01", recorded_at=at(11))

    rows = episodes_per_day_per_robot(db, AUG, SEP)

    assert rows == [
        {"day": date(2026, 8, 10), "robot_id": "arm-01", "episodes": 2},
        {"day": date(2026, 8, 10), "robot_id": "arm-02", "episodes": 1},
        {"day": date(2026, 8, 11), "robot_id": "arm-01", "episodes": 1},
    ]


def test_both_ends_of_the_date_range_are_inclusive(db):
    make_episode(db, "EP-BEFORE", recorded_at=at(9, 23))
    make_episode(db, "EP-FIRST", recorded_at=at(10, 0))
    make_episode(db, "EP-LAST", recorded_at=at(12, 23))
    make_episode(db, "EP-AFTER", recorded_at=at(13, 0))

    rows = episodes_per_day_per_robot(db, date(2026, 8, 10), date(2026, 8, 12))

    assert sum(row["episodes"] for row in rows) == 2
    assert {row["day"] for row in rows} == {date(2026, 8, 10), date(2026, 8, 12)}


# --------------------------------------------------------------------------
# Requests by status
# --------------------------------------------------------------------------


def test_requests_are_counted_by_status_including_empty_ones(db, client_a):
    make_request(db, client_a, status=RequestStatus.submitted)
    make_request(db, client_a, status=RequestStatus.submitted)
    make_request(db, client_a, status=RequestStatus.delivered)

    counts = {row["status"]: row["count"] for row in requests_by_status(db, AUG, date(2030, 1, 1))}

    assert counts[RequestStatus.submitted] == 2
    assert counts[RequestStatus.delivered] == 1
    # Every status is present, so a caller never has to guess at a missing key.
    assert counts[RequestStatus.accepted] == 0
    assert set(counts) == set(RequestStatus)


# --------------------------------------------------------------------------
# Median submitted -> delivered
# --------------------------------------------------------------------------


def deliver_after(db, client_user, hours: float, delivered_on: datetime) -> None:
    """Write a request whose audit trail says it took `hours` to deliver."""
    request = make_request(db, client_user)
    db.query(RequestStatusEvent).filter(
        RequestStatusEvent.request_id == request.id
    ).delete()
    db.add_all(
        [
            RequestStatusEvent(
                request_id=request.id,
                from_status=None,
                to_status=RequestStatus.submitted,
                actor_id=client_user.id,
                created_at=delivered_on - timedelta(hours=hours),
            ),
            RequestStatusEvent(
                request_id=request.id,
                from_status=RequestStatus.in_progress,
                to_status=RequestStatus.delivered,
                actor_id=client_user.id,
                created_at=delivered_on,
            ),
        ]
    )
    db.flush()


def test_median_of_an_odd_number_of_deliveries(db, client_a):
    for hours in (10, 20, 60):
        deliver_after(db, client_a, hours, at(15))

    assert median_submitted_to_delivered_hours(db, AUG, SEP) == 20.0


def test_median_of_an_even_number_interpolates(db, client_a):
    for hours in (10, 20, 30, 60):
        deliver_after(db, client_a, hours, at(15))

    # percentile_cont interpolates between the two middle values.
    assert median_submitted_to_delivered_hours(db, AUG, SEP) == 25.0


def test_median_ignores_requests_that_were_never_delivered(db, client_a):
    deliver_after(db, client_a, 10, at(15))
    deliver_after(db, client_a, 20, at(15))
    make_request(db, client_a, status=RequestStatus.in_progress)  # still open

    assert median_submitted_to_delivered_hours(db, AUG, SEP) == 15.0


def test_median_is_none_when_nothing_was_delivered(db, client_a):
    make_request(db, client_a, status=RequestStatus.submitted)

    assert median_submitted_to_delivered_hours(db, AUG, SEP) is None


def test_median_counts_the_first_delivery_not_a_redelivery(db, client_a):
    """A request that was rejected and delivered again still took as long as
    its first delivery to reach the client."""
    request = make_request(db, client_a)
    db.query(RequestStatusEvent).filter(
        RequestStatusEvent.request_id == request.id
    ).delete()
    db.add_all(
        [
            RequestStatusEvent(
                request_id=request.id,
                from_status=None,
                to_status=RequestStatus.submitted,
                actor_id=client_a.id,
                created_at=at(10),
            ),
            RequestStatusEvent(
                request_id=request.id,
                from_status=RequestStatus.in_progress,
                to_status=RequestStatus.delivered,
                actor_id=client_a.id,
                created_at=at(11),  # 24h
            ),
            RequestStatusEvent(
                request_id=request.id,
                from_status=RequestStatus.rejected,
                to_status=RequestStatus.delivered,
                actor_id=client_a.id,
                created_at=at(20),  # much later
            ),
        ]
    )
    db.flush()

    assert median_submitted_to_delivered_hours(db, AUG, SEP) == 24.0


# --------------------------------------------------------------------------
# Top tasks by good episodes
# --------------------------------------------------------------------------


def test_top_tasks_counts_only_good_episodes(db):
    make_episode(db, "EP-1", task_name="fold towel", quality=Quality.good, recorded_at=at(10))
    make_episode(db, "EP-2", task_name="fold towel", quality=Quality.good, recorded_at=at(10))
    make_episode(db, "EP-3", task_name="pick cup", quality=Quality.good, recorded_at=at(10))
    make_episode(db, "EP-4", task_name="pick cup", quality=Quality.usable, recorded_at=at(10))
    make_episode(db, "EP-5", task_name="pick cup", quality=Quality.bad, recorded_at=at(10))

    rows = top_tasks_by_good_episodes(db, AUG, SEP)

    assert rows == [
        {"task_name": "fold towel", "good_episodes": 2},
        {"task_name": "pick cup", "good_episodes": 1},
    ]


def test_top_tasks_returns_at_most_five(db):
    for index in range(8):
        make_episode(db, f"EP-{index}", task_name=f"task {index}", recorded_at=at(10))

    assert len(top_tasks_by_good_episodes(db, AUG, SEP)) == 5


def test_equal_counts_come_back_in_a_stable_order(db):
    make_episode(db, "EP-1", task_name="beta", recorded_at=at(10))
    make_episode(db, "EP-2", task_name="alpha", recorded_at=at(10))

    rows = top_tasks_by_good_episodes(db, AUG, SEP)

    assert [row["task_name"] for row in rows] == ["alpha", "beta"]


# --------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------


def test_analytics_endpoint_returns_every_section(client, db, operator_user):
    make_episode(db, "EP-1", recorded_at=at(10))

    response = client.get(
        "/api/analytics?from=2026-08-01&to=2026-08-31",
        headers=auth_headers(operator_user),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["episodes_per_day_per_robot"] == [
        {"day": "2026-08-10", "robot_id": "arm-01", "episodes": 1}
    ]
    assert "requests_by_status" in body
    assert "median_submitted_to_delivered_hours" in body
    assert "top_tasks_by_good_episodes" in body


def test_reversed_date_range_is_rejected(client, operator_user):
    response = client.get(
        "/api/analytics?from=2026-08-31&to=2026-08-01",
        headers=auth_headers(operator_user),
    )
    assert response.status_code == 400


def test_an_excessive_date_range_is_rejected(client, operator_user):
    """An unbounded window is a full table scan waiting to happen."""
    response = client.get(
        "/api/analytics?from=2000-01-01&to=2030-01-01",
        headers=auth_headers(operator_user),
    )
    assert response.status_code == 400
