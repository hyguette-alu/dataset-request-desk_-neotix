"""Authorization, enforced on the server.

The brief is explicit that hiding a button in the UI is not enough, so these
tests call the API directly with each role's token and assert on the status
code, never on what a page would render.
"""

import pytest

from tests.conftest import auth_headers, make_request

PROTECTED_ENDPOINTS = [
    ("GET", "/api/auth/me"),
    ("GET", "/api/requests"),
    ("POST", "/api/requests"),
    ("GET", "/api/requests/1"),
    ("POST", "/api/requests/1/status"),
    ("POST", "/api/requests/1/assignments"),
    ("DELETE", "/api/requests/1/assignments/EP-1"),
    ("GET", "/api/episodes"),
    ("POST", "/api/episodes/import"),
    ("GET", "/api/analytics"),
]


@pytest.mark.parametrize("method,path", PROTECTED_ENDPOINTS)
def test_every_endpoint_requires_authentication(client, method, path):
    response = client.request(method, path)
    assert response.status_code == 401, f"{method} {path} was reachable anonymously"


def test_health_is_public(client):
    """The one exception: a health check cannot be behind auth."""
    assert client.get("/api/health").status_code == 200


# --------------------------------------------------------------------------
# A client sees only their own requests
# --------------------------------------------------------------------------


def test_client_list_contains_only_their_own_requests(client, db, client_a, client_b):
    make_request(db, client_a, task_name="mine")
    make_request(db, client_b, task_name="theirs")

    body = client.get("/api/requests", headers=auth_headers(client_a)).json()

    assert body["total"] == 1
    assert [item["task_name"] for item in body["items"]] == ["mine"]


def test_client_cannot_read_another_clients_request(client, db, client_a, client_b):
    theirs = make_request(db, client_b)

    response = client.get(
        f"/api/requests/{theirs.id}", headers=auth_headers(client_a)
    )

    # 404 rather than 403, so the response does not confirm the id exists.
    assert response.status_code == 404


def test_client_cannot_change_another_clients_request(client, db, client_a, client_b):
    theirs = make_request(db, client_b)

    response = client.post(
        f"/api/requests/{theirs.id}/status",
        json={"to_status": "accepted"},
        headers=auth_headers(client_a),
    )

    assert response.status_code == 404


def test_client_cannot_create_a_request_for_someone_else(client, db, client_a):
    """Ownership comes from the token, not the payload, so there is no field
    to tamper with."""
    response = client.post(
        "/api/requests",
        json={
            "task_name": "pick cup",
            "episodes_requested": 1,
            "deadline": "2026-12-31",
            "client_id": 9999,  # ignored
        },
        headers=auth_headers(client_a),
    )

    assert response.status_code == 201
    assert response.json()["client"]["id"] == client_a.id


# --------------------------------------------------------------------------
# Operators and admins
# --------------------------------------------------------------------------


def test_operator_sees_every_clients_requests(
    client, db, client_a, client_b, operator_user
):
    make_request(db, client_a)
    make_request(db, client_b)

    body = client.get("/api/requests", headers=auth_headers(operator_user)).json()

    assert body["total"] == 2


def test_operator_cannot_create_a_request(client, operator_user):
    """Requests belong to clients; an operator has no client to own one."""
    response = client.post(
        "/api/requests",
        json={
            "task_name": "pick cup",
            "episodes_requested": 1,
            "deadline": "2026-12-31",
        },
        headers=auth_headers(operator_user),
    )

    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/api/episodes", "/api/analytics"])
def test_clients_cannot_reach_operator_endpoints(client, client_a, path):
    response = client.get(path, headers=auth_headers(client_a))
    assert response.status_code == 403


def test_clients_cannot_import_episodes(client, client_a):
    response = client.post(
        "/api/episodes/import",
        files={"file": ("x.csv", b"episode_id\n", "text/csv")},
        headers=auth_headers(client_a),
    )
    assert response.status_code == 403


def test_admin_can_do_everything_an_operator_can(client, db, client_a, admin_user):
    make_request(db, client_a)

    assert client.get("/api/requests", headers=auth_headers(admin_user)).status_code == 200
    assert client.get("/api/episodes", headers=auth_headers(admin_user)).status_code == 200
    assert client.get("/api/analytics", headers=auth_headers(admin_user)).status_code == 200
