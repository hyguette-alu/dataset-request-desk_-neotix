import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.deps import get_current_user
from app.models import User
from app.schemas import LoginRequest, UserOut
from app.security import create_access_token, verify_password

logger = logging.getLogger("app.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        httponly=True,  # not readable from JavaScript, so XSS cannot steal it
        samesite="lax",  # not sent on cross-site POSTs, which covers CSRF here
        secure=settings.cookie_secure,  # True behind TLS
        max_age=settings.access_token_ttl_minutes * 60,
        path="/",
    )


@router.post("/login", response_model=UserOut)
def login(
    payload: LoginRequest, response: Response, db: Session = Depends(get_db)
) -> User:
    email = payload.email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))

    # One message and one status for every failure mode (no such user, wrong
    # password, deactivated account) so the endpoint cannot be used to find out
    # which addresses are registered.
    if user is None or not user.is_active or not verify_password(
        payload.password, user.password_hash
    ):
        logger.warning("login_failed", extra={"email": email})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    token = create_access_token(user_id=user.id, role=user.role.value)
    _set_session_cookie(response, token)
    logger.info("login_succeeded", extra={"user_id": user.id, "role": user.role.value})
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    """Clear the session cookie.

    The JWT itself stays valid until it expires; see NOTES.md for why I
    accepted that and what revocation would require.
    """
    response.delete_cookie(key=settings.cookie_name, path="/")


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> User:
    return user
