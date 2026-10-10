"""Authentication routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import User
from ..security import create_access_token, verify_password

router = APIRouter(prefix="/api", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    username: str
    role: str
    display_name: str


class LoginOut(BaseModel):
    token: str
    user: UserOut


def _user_out(user: User) -> UserOut:
    return UserOut(
        username=user.username,
        role=user.role,
        display_name=user.display_name or user.username,
    )


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn, db: Session = Depends(get_db)) -> LoginOut:
    user = db.query(User).filter(User.username == body.username.strip()).first()
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    token = create_access_token(user.username, user.role, {"name": user.display_name})
    return LoginOut(token=token, user=_user_out(user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return _user_out(user)
