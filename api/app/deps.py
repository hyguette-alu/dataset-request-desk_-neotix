"""Authentication and authorization dependencies.

Every protected route declares its required roles here. Authorization is a
server-side dependency on the route, never a UI concern: hiding a button in
React changes nothing about what the API will accept.
"""

from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Role, User
from app.security import InvalidToken, decode_access_token

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def _extract_token(request: Request) -> str | None:
    """Accept the token from either transport.

    The React app is served same-origin and authenticates with an HttpOnly
    cookie, so no token is reachable from JavaScript. The Authorization header
    is also accepted so the API can be driven from curl or a script.
    """
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return request.cookies.get(settings.cookie_name)


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    token = _extract_token(request)
    if not token:
        raise _UNAUTHENTICATED

    try:
        payload = decode_access_token(token)
    except InvalidToken:
        raise _UNAUTHENTICATED from None

    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        raise _UNAUTHENTICATED from None

    user = db.get(User, user_id)
    # A deactivated user's existing token must stop working immediately,
    # so active status is checked against the database on every request
    # rather than trusted from the token.
    if user is None or not user.is_active:
        raise _UNAUTHENTICATED

    # Picked up by the access-log middleware in app.main.
    request.state.user_id = user.id
    return user


def require_role(*roles: Role) -> Callable[[User], User]:
    """Route dependency factory: allow only these roles."""
    allowed = set(roles)

    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your role is not permitted to perform this action",
            )
        return user

    return dependency


# Named dependencies for the three common cases.
require_client = require_role(Role.client)
require_operator = require_role(Role.operator, Role.admin)
require_admin = require_role(Role.admin)
