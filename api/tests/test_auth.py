"""Authentication behaviour.

Authorization rules that depend on requests and assignments live in the tests
for those features; this file covers login, identity and token handling.
"""

from app.models import Role
from tests.conftest import auth_headers, make_user


def test_login_succeeds_and_sets_httponly_cookie(client, db):
    make_user(db, "someone@example.com", Role.client, password="correct-horse")

    response = client.post(
        "/api/auth/login",
        json={"email": "someone@example.com", "password": "correct-horse"},
    )

    assert response.status_code == 200
    assert response.json()["email"] == "someone@example.com"
    # The token must not be reachable from JavaScript.
    assert "httponly" in response.headers["set-cookie"].lower()


def test_login_is_case_insensitive_on_email(client, db):
    make_user(db, "mixed@example.com", Role.client, password="correct-horse")

    response = client.post(
        "/api/auth/login",
        json={"email": "MiXeD@EXAMPLE.com", "password": "correct-horse"},
    )

    assert response.status_code == 200


def test_login_with_wrong_password_is_rejected(client, db):
    make_user(db, "someone@example.com", Role.client, password="correct-horse")

    response = client.post(
        "/api/auth/login",
        json={"email": "someone@example.com", "password": "wrong"},
    )

    assert response.status_code == 401


def test_unknown_email_and_wrong_password_are_indistinguishable(client, db):
    """No user enumeration: both failures look identical to a caller."""
    make_user(db, "someone@example.com", Role.client, password="correct-horse")

    wrong_password = client.post(
        "/api/auth/login",
        json={"email": "someone@example.com", "password": "wrong"},
    )
    no_such_user = client.post(
        "/api/auth/login",
        json={"email": "nobody@example.com", "password": "wrong"},
    )

    assert wrong_password.status_code == no_such_user.status_code == 401
    assert wrong_password.json() == no_such_user.json()


def test_deactivated_user_cannot_log_in(client, db):
    make_user(
        db, "gone@example.com", Role.client, password="correct-horse", is_active=False
    )

    response = client.post(
        "/api/auth/login",
        json={"email": "gone@example.com", "password": "correct-horse"},
    )

    assert response.status_code == 401


def test_deactivated_user_existing_token_stops_working(client, db):
    """Active status is checked against the database, not trusted from the
    token, so deactivation takes effect before the token expires."""
    user = make_user(db, "revoked@example.com", Role.operator)
    headers = auth_headers(user)

    assert client.get("/api/auth/me", headers=headers).status_code == 200

    user.is_active = False
    db.flush()

    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_me_requires_authentication(client):
    assert client.get("/api/auth/me").status_code == 401


def test_garbage_token_is_rejected(client):
    response = client.get(
        "/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


def test_token_signed_with_another_secret_is_rejected(client, db):
    """Guards against the classic mistake of trusting an unverified payload."""
    import jwt

    user = make_user(db, "someone@example.com", Role.operator)
    forged = jwt.encode(
        {"sub": str(user.id), "role": "admin", "exp": 9999999999},
        "not-the-real-secret",
        algorithm="HS256",
    )

    response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401
