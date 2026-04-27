"""FastAPI dependencies for user authentication (cookie-based, multi-user)."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Request, status

from kimi_cli.web.db.crud import get_user_session
from kimi_cli.web.db.database import get_db

_COOKIE_NAME = "kimi_session"


def get_current_user(request: Request) -> dict[str, Any] | None:
    """Return the currently authenticated user dict, or ``None``.

    Authentication sources (in priority order):
    1. Cookie ``kimi_session`` (primary, used by browser clients).
    2. ``Authorization: Bearer <token>`` header (for API / admin clients).
    """
    token: str | None = None

    # 1. Cookie
    cookie_token = request.cookies.get(_COOKIE_NAME)
    if cookie_token:
        token = cookie_token

    # 2. Bearer header fallback
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip() or None

    if not token:
        return None

    try:
        db = get_db()
        try:
            user = get_user_session(db, token)
        finally:
            db.close()
        return user
    except Exception:
        return None


def require_current_user(
    user: dict[str, Any] | None = Depends(get_current_user),  # noqa: B008
) -> dict[str, Any]:
    """Require an authenticated user; raise 401 if not logged in."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return user


def require_admin(
    user: dict[str, Any] = Depends(require_current_user),  # noqa: B008
) -> dict[str, Any]:
    """Require admin role; raise 403 if the user is not an admin."""
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user


__all__ = [
    "get_current_user",
    "require_admin",
    "require_current_user",
]
