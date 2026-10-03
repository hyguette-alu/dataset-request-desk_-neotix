"""Assignment rules.

* only `good` or `usable` episodes may be attached;
* an episode belongs to at most one request at a time;
* episodes may only be attached while the request is still being worked on.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Assignment, Quality, RequestStatus
from tests.conftest import assign, auth_headers, make_episode, make_request


def post_assignment(client, user, request_id, episode_ids):
    return client.post(
        f"/api/requests/{request_id}/assignments",
        json={"episode_ids": episode_ids},
        headers=auth_headers(user),
    )


# --------------------------------------------------------------------------
# Quality
# --------------------------------------------------------------------------


@pytest.mark.parametrize("quality", [Quality.good, Quality.usable])
def test_good_and_usable_episodes_can_be_assigned(
    client, db, client_a, operator_user, quality
):
    request = make_request(db, client_a)
    make_episode(db, "EP-1", quality=quality)

    response = post_assignment(client, operator_user, request.id, ["EP-1"])

    assert response.status_code == 200
    assert response.json()["assigned_count"] == 1


def test_bad_episodes_cannot_be_assigned(client, db, client_a, operator_user):
    request = make_request(db, client_a)
    make_episode(db, "EP-BAD", quality=Quality.bad)

    response = post_assignment(client, operator_user, request.id, ["EP-BAD"])

    assert response.status_code == 409
    assert response.json()["error"] == "AssignmentRejected"


def test_a_bad_episode_in_the_batch_rejects_the_whole_batch(
    client, db, client_a, operator_user
):
    """All or nothing, so an operator is never left guessing which half of
    their selection went through."""
    request = make_request(db, client_a)
    make_episode(db, "EP-OK", quality=Quality.good)
    make_episode(db, "EP-BAD", quality=Quality.bad)

    response = post_assignment(client, operator_user, request.id, ["EP-OK", "EP-BAD"])

    assert response.status_code == 409
    assert db.query(Assignment).count() == 0


# --------------------------------------------------------------------------
# One request per episode
# --------------------------------------------------------------------------


def test_an_episode_cannot_be_assigned_to_two_requests(
    client, db, client_a, client_b, operator_user
):
    first = make_request(db, client_a)
    second = make_request(db, client_b)
    make_episode(db, "EP-1")

    assert post_assignment(client, operator_user, first.id, ["EP-1"]).status_code == 200

    response = post_assignment(client, operator_user, second.id, ["EP-1"])

    assert response.status_code == 409
    assert "already assigned" in response.json()["detail"]


def test_assigning_the_same_episode_twice_to_one_request_is_refused(
    client, db, client_a, operator_user
):
    request = make_request(db, client_a)
    make_episode(db, "EP-1")
    post_assignment(client, operator_user, request.id, ["EP-1"])

    response = post_assignment(client, operator_user, request.id, ["EP-1"])

    assert response.status_code == 409


def test_duplicate_ids_within_one_call_are_collapsed(
    client, db, client_a, operator_user
):
    request = make_request(db, client_a)
    make_episode(db, "EP-1")

    response = post_assignment(client, operator_user, request.id, ["EP-1", "EP-1"])

    assert response.status_code == 200
    assert response.json()["assigned_count"] == 1


def test_the_database_enforces_one_request_per_episode(db, client_a, client_b, operator_user):
    """The service checks first to give a decent error, but the UNIQUE
    constraint is what makes the rule true under concurrency. If this test
    ever fails, the service check has become the only thing holding the line.
    """
    first = make_request(db, client_a)
    second = make_request(db, client_b)
    episode = make_episode(db, "EP-1")

    assign(db, first, [episode], operator_user)

    db.add(
        Assignment(
            request_id=second.id, episode_id=episode.episode_id, assigned_by=operator_user.id
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()


def test_unassigning_frees_the_episode_for_another_request(
    client, db, client_a, client_b, operator_user
):
    first = make_request(db, client_a)
    second = make_request(db, client_b)
    make_episode(db, "EP-1")
    post_assignment(client, operator_user, first.id, ["EP-1"])

    removed = client.delete(
        f"/api/requests/{first.id}/assignments/EP-1",
        headers=auth_headers(operator_user),
    )
    assert removed.status_code == 204

    assert post_assignment(client, operator_user, second.id, ["EP-1"]).status_code == 200


# --------------------------------------------------------------------------
# When assignment is allowed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [RequestStatus.submitted, RequestStatus.in_progress, RequestStatus.rejected],
)
def test_episodes_can_be_assigned_while_the_work_is_open(
    client, db, client_a, operator_user, status
):
    request = make_request(db, client_a, status=status)
    make_episode(db, "EP-1")

    assert post_assignment(client, operator_user, request.id, ["EP-1"]).status_code == 200


@pytest.mark.parametrize(
    "status", [RequestStatus.delivered, RequestStatus.accepted]
)
def test_episodes_cannot_be_assigned_once_delivered(
    client, db, client_a, operator_user, status
):
    """After delivery the episode set is what the client is reviewing, so it
    is frozen until they reject it and it returns to in_progress."""
    request = make_request(db, client_a, status=status)
    make_episode(db, "EP-1")

    response = post_assignment(client, operator_user, request.id, ["EP-1"])

    assert response.status_code == 409


# --------------------------------------------------------------------------
# Who may assign, and what may be assigned
# --------------------------------------------------------------------------


def test_clients_cannot_assign_episodes(client, db, client_a):
    request = make_request(db, client_a)
    make_episode(db, "EP-1")

    response = post_assignment(client, client_a, request.id, ["EP-1"])

    assert response.status_code == 403


def test_unknown_episode_is_a_404(client, db, client_a, operator_user):
    request = make_request(db, client_a)

    response = post_assignment(client, operator_user, request.id, ["EP-NOPE"])

    assert response.status_code == 404


def test_episode_ids_are_matched_case_insensitively(
    client, db, client_a, operator_user
):
    """Ids are stored upper-cased on import, so `ep-1` must find `EP-1`."""
    request = make_request(db, client_a)
    make_episode(db, "EP-1")

    assert post_assignment(client, operator_user, request.id, ["ep-1"]).status_code == 200


def test_empty_episode_list_is_rejected_by_validation(
    client, db, client_a, operator_user
):
    request = make_request(db, client_a)

    response = post_assignment(client, operator_user, request.id, [])

    assert response.status_code == 422
