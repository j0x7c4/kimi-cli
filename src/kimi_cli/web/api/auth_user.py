"""User authentication API endpoints (login / logout / me)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.requests import Request
from pydantic import BaseModel

from kimi_cli.web.db.crud import (
    create_user_session,
    delete_user_session,
    get_user_by_username,
    verify_password,
)
from kimi_cli.web.db.database import get_db
from kimi_cli.web.user_auth import require_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])

_COOKIE_NAME = "kimi_session"
_SESSION_MAX_AGE = 86400  # 24 hours in seconds


class LoginRequest(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    user_id: str
    username: str
    role: str


@router.post("/login", summary="Log in and obtain a session cookie")
async def login(body: LoginRequest, response: Response) -> UserResponse:
    """Authenticate with username and password.

    On success, sets an ``HttpOnly`` ``kimi_session`` cookie and returns basic
    user information.  Returns 401 on invalid credentials or inactive account.
    """
    with get_db() as db:
        user = get_user_by_username(db, body.username)
        if user is None or not verify_password(body.password, user["password_hash"]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password",
            )
        if not user.get("is_active"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Account is disabled",
            )
        token = create_user_session(db, user["id"], expires_in_seconds=_SESSION_MAX_AGE)

    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=_SESSION_MAX_AGE,
    )
    return UserResponse(
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
    )


@router.post("/logout", summary="Log out and clear the session cookie")
async def logout(request: Request, response: Response) -> dict[str, str]:
    """Invalidate the current session and clear the session cookie."""
    token = request.cookies.get(_COOKIE_NAME)
    if token:
        try:
            with get_db() as db:
                delete_user_session(db, token)
        except Exception:
            pass

    response.delete_cookie(key=_COOKIE_NAME, path="/")
    return {"detail": "Logged out"}


@router.get("/me", summary="Get current authenticated user")
async def me(
    user: dict[str, Any] = Depends(require_current_user),
) -> UserResponse:
    """Return the currently authenticated user.  Returns 401 if not logged in."""
    return UserResponse(
        user_id=user["id"],
        username=user["username"],
        role=user["role"],
    )


__all__ = ["router"]
