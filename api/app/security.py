"""Password hashing and JWT issuing/verification.

Passwords: bcrypt with a per-password salt, cost factor left at the library
default (12). Plaintext never reaches the database.

Tokens: short-lived HS256 JWTs. They are stateless, which means logout is
client-side only and a stolen token stays valid until it expires; the
trade-off and what I would do instead are discussed in NOTES.md.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.config import settings

# bcrypt hashes at most 72 bytes and silently ignores the rest, which would
# make two different long passwords interchangeable. We reject instead.
MAX_PASSWORD_BYTES = 72


class InvalidToken(Exception):
    pass


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(f"password must be at most {MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(encoded, password_hash.encode("utf-8"))
    except ValueError:
        # Malformed hash in the database; treat as a failed login rather than
        # a 500, but it is worth alerting on in a real system.
        return False


def create_access_token(user_id: int, role: str) -> str:
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_ttl_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidToken(str(exc)) from exc
