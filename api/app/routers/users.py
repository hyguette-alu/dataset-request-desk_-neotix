"""User administration. Admin only.

Accounts are deactivated, never deleted: `users.id` is referenced by
`requests.client_id` and `request_status_events.actor_id` with ON DELETE
RESTRICT, because removing a user would either orphan or rewrite the audit
trail. `is_active` is checked on every authenticated request, so clearing it
takes effect immediately rather than when the user's token expires.

Two guards stop an admin locking everyone out, including themselves:

  * you cannot deactivate or demote your own account;
  * the last active admin cannot be deactivated or demoted by anyone.
"""

import logging

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_admin
from app.errors import DomainError, NotFound, PermissionDenied
from app.models import Role, User
from app.schemas import UserCreate, UserOut, UserUpdate
from app.security import hash_password

logger = logging.getLogger("app.users")

router = APIRouter(prefix="/api/users", tags=["users"])


class EmailAlreadyUsed(DomainError):
    status_code = 409


def _count_other_active_admins(db: Session, user_id: int) -> int:
    return db.scalar(
        select(func.count())
        .select_from(User)
        .where(
            User.role == Role.admin,
            User.is_active.is_(True),
            User.id != user_id,
        )
    ) or 0


@router.get("", response_model=list[UserOut])
def list_users(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
    include_inactive: bool = Query(default=True),
) -> list[User]:
    statement = select(User).order_by(User.role, User.email)
    if not include_inactive:
        statement = statement.where(User.is_active.is_(True))
    return list(db.scalars(statement))


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
) -> User:
    email = payload.email.strip().lower()

    # Checked up front for a decent error; the UNIQUE index on users.email is
    # what actually guarantees it under concurrency.
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise EmailAlreadyUsed(f"{email} already has an account")

    user = User(
        email=email,
        name=payload.name.strip(),
        organisation=(payload.organisation or "").strip() or None,
        role=payload.role,
        password_hash=hash_password(payload.password),
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    logger.info(
        "user_created",
        extra={"user_id": user.id, "role": user.role.value, "actor_id": actor.id},
    )
    return user


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    actor: User = Depends(require_admin),
) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise NotFound(f"user {user_id} not found")

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return user

    losing_admin = (
        changes.get("is_active") is False
        or ("role" in changes and changes["role"] is not Role.admin)
    )

    if losing_admin and user.id == actor.id:
        raise PermissionDenied(
            "you cannot deactivate or demote your own admin account"
        )

    if (
        losing_admin
        and user.role is Role.admin
        and user.is_active
        and _count_other_active_admins(db, user.id) == 0
    ):
        raise DomainError("this is the last active admin account")

    for field, value in changes.items():
        if field == "organisation":
            value = (value or "").strip() or None
        if field == "name" and value is not None:
            value = value.strip()
        setattr(user, field, value)

    db.commit()
    db.refresh(user)

    logger.info(
        "user_updated",
        extra={
            "user_id": user.id,
            "changed": sorted(changes),
            "actor_id": actor.id,
        },
    )
    return user
