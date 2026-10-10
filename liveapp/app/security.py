"""Password hashing and JWT helpers."""

from __future__ import annotations

import datetime as dt
from typing import Any

import bcrypt
import jwt

from .config import settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"), password_hash.encode("utf-8")
        )
    except (ValueError, TypeError):
        return False


def create_access_token(
    username: str, role: str, extra: dict[str, Any] | None = None
) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload: dict[str, Any] = {
        "sub": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(minutes=settings.token_ttl_minutes)).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
