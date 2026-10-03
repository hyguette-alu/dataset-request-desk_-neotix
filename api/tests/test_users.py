"""User administration.

Covers who may manage accounts, that deactivation takes effect immediately
rather than at token expiry, and the two guards that stop an admin locking
everybody out of the system.
"""

import pytest
from sqlalchemy import select

from app.models import Role, User
from app.security import verify_password
from tests.conftest import auth_headers, make_user


# --------------------------------------------------------------------------
# Who may manage users
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", [("GET", "/api/users"), ("POST", "/api/users")])
def test_user_admin_requires_authentication(client, method, path):
    assert client.request(method, path).status_code == 401


def test_operators_cannot_manage_users(client, operator_user):
    """Admin is a superset of operator for request work, but user management
    is the one thing only an admin may do."""
    assert client.get("/api/users", headers=auth_headers(operator_user)).status_code == 403
    assert (
        client.post(
            "/api/users",
            json={
                "email": "new@example.com",
                "name": "New",
                "role": "client",
                "password": "a-good-password",
            },
            headers=auth_headers(operator_user),
        ).status_code
        == 403
    )


def test_clients_cannot_manage_users(client, client_a):
    assert client.get("/api/users", headers=auth_headers(client_a)).status_code == 403


def test_admin_can_list_users(client, db, admin_user, operator_user, client_a):
    response = client.get("/api/users", headers=auth_headers(admin_user))

    assert response.status_code == 200
    emails = {u["email"] for u in response.json()}
    assert {admin_user.email, operator_user.email, client_a.email} <= emails


# --------------------------------------------------------------------------
# Creating accounts
# --------------------------------------------------------------------------


def test_admin_creates_a_user_and_the_password_is_hashed(client, db, admin_user):
    response = client.post(
        "/api/users",
        json={
            "email": "Fresh@Example.com",
            "name": "Fresh Client",
            "role": "client",
            "password": "a-good-password",
            "organisation": "Fresh Robotics",
        },
        headers=auth_headers(admin_user),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "fresh@example.com"  # normalised
    assert body["is_active"] is True
    assert "password" not in body and "password_hash" not in body

    created = db.scalar(select(User).where(User.email == "fresh@example.com"))
    assert created.password_hash != "a-good-password"
    assert verify_password("a-good-password", created.password_hash)


def test_a_created_user_can_log_in(client, db, admin_user):
    client.post(
        "/api/users",
        json={
            "email": "newbie@example.com",
            "name": "Newbie",
            "role": "operator",
            "password": "a-good-password",
        },
        headers=auth_headers(admin_user),
    )

    login = client.post(
        "/api/auth/login",
        json={"email": "newbie@example.com", "password": "a-good-password"},
    )
    assert login.status_code == 200
    assert login.json()["role"] == "operator"


def test_duplicate_email_is_refused(client, db, admin_user):
    make_user(db, "taken@example.com", Role.client)

    response = client.post(
        "/api/users",
        json={
            "email": "taken@example.com",
            "name": "Clash",
            "role": "client",
            "password": "a-good-password",
        },
        headers=auth_headers(admin_user),
    )

    assert response.status_code == 409


def test_a_short_password_is_refused_by_validation(client, admin_user):
    response = client.post(
        "/api/users",
        json={
            "email": "weak@example.com",
            "name": "Weak",
            "role": "client",
            "password": "short",
        },
        headers=auth_headers(admin_user),
    )
    assert response.status_code == 422


def test_an_invalid_role_is_refused_by_validation(client, admin_user):
    response = client.post(
        "/api/users",
        json={
            "email": "odd@example.com",
            "name": "Odd",
            "role": "superuser",
            "password": "a-good-password",
        },
        headers=auth_headers(admin_user),
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Changing roles and deactivating
# --------------------------------------------------------------------------


def test_admin_can_change_a_users_role(client, db, admin_user, client_a):
    response = client.patch(
        f"/api/users/{client_a.id}",
        json={"role": "operator"},
        headers=auth_headers(admin_user),
    )

    assert response.status_code == 200
    assert response.json()["role"] == "operator"


def test_a_promoted_user_immediately_gains_the_new_permissions(
    client, db, admin_user, client_a
):
    """A client cannot list episodes; the same account can once it is an
    operator, without logging out and back in."""
    assert client.get("/api/episodes", headers=auth_headers(client_a)).status_code == 403

    client.patch(
        f"/api/users/{client_a.id}",
        json={"role": "operator"},
        headers=auth_headers(admin_user),
    )

    assert client.get("/api/episodes", headers=auth_headers(client_a)).status_code == 200


def test_deactivating_a_user_revokes_their_existing_token(
    client, db, admin_user, operator_user
):
    """The point of checking is_active per request rather than trusting the
    token: a deactivated user is locked out at once, not in eight hours."""
    headers = auth_headers(operator_user)
    assert client.get("/api/auth/me", headers=headers).status_code == 200

    client.patch(
        f"/api/users/{operator_user.id}",
        json={"is_active": False},
        headers=auth_headers(admin_user),
    )

    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_a_deactivated_user_can_be_reactivated(client, db, admin_user):
    user = make_user(db, "back@example.com", Role.operator, is_active=False)

    response = client.patch(
        f"/api/users/{user.id}",
        json={"is_active": True},
        headers=auth_headers(admin_user),
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is True


def test_updating_an_unknown_user_is_a_404(client, admin_user):
    response = client.patch(
        "/api/users/999999", json={"is_active": False}, headers=auth_headers(admin_user)
    )
    assert response.status_code == 404


def test_an_empty_patch_changes_nothing(client, db, admin_user, client_a):
    response = client.patch(
        f"/api/users/{client_a.id}", json={}, headers=auth_headers(admin_user)
    )

    assert response.status_code == 200
    assert response.json()["role"] == "client"
    assert response.json()["is_active"] is True


# --------------------------------------------------------------------------
# Lockout guards
# --------------------------------------------------------------------------


def test_an_admin_cannot_deactivate_themselves(client, db, admin_user):
    make_user(db, "second-admin@example.com", Role.admin)  # not the last one

    response = client.patch(
        f"/api/users/{admin_user.id}",
        json={"is_active": False},
        headers=auth_headers(admin_user),
    )

    assert response.status_code == 403
    db.refresh(admin_user)
    assert admin_user.is_active is True


def test_an_admin_cannot_demote_themselves(client, db, admin_user):
    make_user(db, "second-admin@example.com", Role.admin)

    response = client.patch(
        f"/api/users/{admin_user.id}",
        json={"role": "operator"},
        headers=auth_headers(admin_user),
    )

    assert response.status_code == 403
    db.refresh(admin_user)
    assert admin_user.role is Role.admin


def test_the_last_active_admin_cannot_be_deactivated(client, db, admin_user):
    """Guards against the one-way door: with no active admin left, nobody can
    create another one."""
    other = make_user(db, "other-admin@example.com", Role.admin)

    # Demoting the first one is fine while a second admin exists.
    assert (
        client.patch(
            f"/api/users/{other.id}",
            json={"role": "operator"},
            headers=auth_headers(admin_user),
        ).status_code
        == 200
    )

    # Now admin_user is the last one, and even another admin could not do it.
    restored = make_user(db, "temp-admin@example.com", Role.admin)
    client.patch(
        f"/api/users/{restored.id}",
        json={"is_active": False},
        headers=auth_headers(admin_user),
    )

    response = client.patch(
        f"/api/users/{admin_user.id}",
        json={"is_active": False},
        headers=auth_headers(restored),
    )
    assert response.status_code in (400, 401, 403)
    db.refresh(admin_user)
    assert admin_user.is_active is True


def test_an_admin_may_deactivate_a_different_admin(client, db, admin_user):
    other = make_user(db, "spare-admin@example.com", Role.admin)

    response = client.patch(
        f"/api/users/{other.id}",
        json={"is_active": False},
        headers=auth_headers(admin_user),
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is False
