"""The request status workflow.

    submitted -> in_progress -> delivered -> accepted
                                         \\-> rejected -> in_progress

These tests cover the three ways a transition can be wrong: the edge does not
exist, the caller's role does not own the edge, and the request is not ready
(delivering under the requested episode count).
"""

import pytest

from app.models import Quality, RequestStatus
from tests.conftest import assign, auth_headers, make_episode, make_request


def change_status(client, user, request_id, to_status, note=None):
    return client.post(
        f"/api/requests/{request_id}/status",
        json={"to_status": to_status.value, "note": note},
        headers=auth_headers(user),
    )


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_full_lifecycle_submitted_to_accepted(client, db, client_a, operator_user):
    request = make_request(db, client_a, episodes_requested=2)
    episodes = [make_episode(db, "EP-1"), make_episode(db, "EP-2")]
    assign(db, request, episodes, operator_user)

    assert change_status(
        client, operator_user, request.id, RequestStatus.in_progress
    ).status_code == 200
    assert change_status(
        client, operator_user, request.id, RequestStatus.delivered
    ).status_code == 200

    response = change_status(client, client_a, request.id, RequestStatus.accepted)
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


def test_rejection_sends_the_request_back_for_rework(
    client, db, client_a, operator_user
):
    request = make_request(db, client_a, episodes_requested=1)
    assign(db, request, [make_episode(db, "EP-1")], operator_user)

    change_status(client, operator_user, request.id, RequestStatus.in_progress)
    change_status(client, operator_user, request.id, RequestStatus.delivered)

    rejected = change_status(
        client, client_a, request.id, RequestStatus.rejected, note="wrong angle"
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    rework = change_status(
        client, operator_user, request.id, RequestStatus.in_progress
    )
    assert rework.status_code == 200


# --------------------------------------------------------------------------
# Edges that do not exist
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start,target",
    [
        # Skipping a step.
        (RequestStatus.submitted, RequestStatus.delivered),
        (RequestStatus.submitted, RequestStatus.accepted),
        (RequestStatus.in_progress, RequestStatus.accepted),
        # Going backwards.
        (RequestStatus.in_progress, RequestStatus.submitted),
        (RequestStatus.delivered, RequestStatus.in_progress),
        # Leaving a terminal state.
        (RequestStatus.accepted, RequestStatus.in_progress),
        (RequestStatus.accepted, RequestStatus.rejected),
        # Staying put.
        (RequestStatus.submitted, RequestStatus.submitted),
    ],
)
def test_invalid_transitions_are_refused(
    client, db, client_a, admin_user, start, target
):
    """An admin is used so the refusal is about the edge, not the role."""
    request = make_request(db, client_a, episodes_requested=1, status=start)

    response = change_status(client, admin_user, request.id, target)

    assert response.status_code == 409
    assert response.json()["error"] == "InvalidTransition"


def test_refused_transition_does_not_change_the_status(client, db, client_a, admin_user):
    request = make_request(db, client_a, status=RequestStatus.submitted)

    change_status(client, admin_user, request.id, RequestStatus.delivered)
    db.refresh(request)

    assert request.status is RequestStatus.submitted


# --------------------------------------------------------------------------
# Edges that exist but belong to another role
# --------------------------------------------------------------------------


def test_client_cannot_start_work_on_their_own_request(client, db, client_a):
    request = make_request(db, client_a)

    response = change_status(client, client_a, request.id, RequestStatus.in_progress)

    assert response.status_code == 403


def test_client_cannot_deliver_their_own_request(client, db, client_a, operator_user):
    request = make_request(db, client_a, episodes_requested=1)
    assign(db, request, [make_episode(db, "EP-1")], operator_user)
    change_status(client, operator_user, request.id, RequestStatus.in_progress)

    response = change_status(client, client_a, request.id, RequestStatus.delivered)

    assert response.status_code == 403


@pytest.mark.parametrize("target", [RequestStatus.accepted, RequestStatus.rejected])
def test_operator_cannot_accept_or_reject_on_the_clients_behalf(
    client, db, client_a, operator_user, target
):
    request = make_request(db, client_a, status=RequestStatus.delivered)

    response = change_status(client, operator_user, request.id, target)

    assert response.status_code == 403


@pytest.mark.parametrize("target", [RequestStatus.accepted, RequestStatus.rejected])
def test_admin_cannot_accept_or_reject_either(
    client, db, client_a, admin_user, target
):
    """Admin is a superset of operator, not of client. Accepting a delivery is
    the client's commercial decision and an admin standing in for them would
    defeat the point of recording who accepted it."""
    request = make_request(db, client_a, status=RequestStatus.delivered)

    response = change_status(client, admin_user, request.id, target)

    assert response.status_code == 403


def test_a_client_cannot_accept_another_clients_delivery(
    client, db, client_a, client_b
):
    request = make_request(db, client_a, status=RequestStatus.delivered)

    response = change_status(client, client_b, request.id, RequestStatus.accepted)

    # 404, not 403: client B is not entitled to learn that this request exists.
    assert response.status_code == 404


# --------------------------------------------------------------------------
# The delivery precondition
# --------------------------------------------------------------------------


def test_cannot_deliver_with_too_few_episodes_assigned(
    client, db, client_a, operator_user
):
    request = make_request(db, client_a, episodes_requested=3)
    assign(db, request, [make_episode(db, "EP-1")], operator_user)
    change_status(client, operator_user, request.id, RequestStatus.in_progress)

    response = change_status(client, operator_user, request.id, RequestStatus.delivered)

    assert response.status_code == 409
    body = response.json()
    assert body["assigned"] == 1
    assert body["required"] == 3


def test_can_deliver_once_the_count_is_met(client, db, client_a, operator_user):
    request = make_request(db, client_a, episodes_requested=2)
    assign(
        db, request, [make_episode(db, "EP-1"), make_episode(db, "EP-2")], operator_user
    )
    change_status(client, operator_user, request.id, RequestStatus.in_progress)

    assert change_status(
        client, operator_user, request.id, RequestStatus.delivered
    ).status_code == 200


def test_can_deliver_with_more_episodes_than_requested(
    client, db, client_a, operator_user
):
    """`episodes_requested` is a minimum, not an exact quota."""
    request = make_request(db, client_a, episodes_requested=1)
    assign(
        db,
        request,
        [make_episode(db, "EP-1"), make_episode(db, "EP-2", quality=Quality.usable)],
        operator_user,
    )
    change_status(client, operator_user, request.id, RequestStatus.in_progress)

    assert change_status(
        client, operator_user, request.id, RequestStatus.delivered
    ).status_code == 200


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------


def test_every_status_change_records_who_and_when(
    client, db, client_a, operator_user
):
    request = make_request(db, client_a, episodes_requested=1)
    assign(db, request, [make_episode(db, "EP-1")], operator_user)

    change_status(client, operator_user, request.id, RequestStatus.in_progress)
    change_status(client, operator_user, request.id, RequestStatus.delivered)
    change_status(client, client_a, request.id, RequestStatus.rejected, note="blurry")

    history = client.get(
        f"/api/requests/{request.id}", headers=auth_headers(operator_user)
    ).json()["history"]

    assert [(e["from_status"], e["to_status"]) for e in history] == [
        (None, "submitted"),
        ("submitted", "in_progress"),
        ("in_progress", "delivered"),
        ("delivered", "rejected"),
    ]
    assert history[1]["actor_id"] == operator_user.id
    assert history[3]["actor_id"] == client_a.id
    assert history[3]["note"] == "blurry"
    assert all(event["created_at"] for event in history)


def test_a_refused_transition_leaves_no_audit_row(client, db, client_a, admin_user):
    request = make_request(db, client_a, status=RequestStatus.submitted)

    change_status(client, admin_user, request.id, RequestStatus.accepted)

    history = client.get(
        f"/api/requests/{request.id}", headers=auth_headers(admin_user)
    ).json()["history"]
    assert len(history) == 1  # only the opening "submitted" row
