"""Admin API endpoints for user management."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from kimi_cli.web.db.crud import (
    create_user,
    delete_user,
    list_users,
    update_user,
)
from kimi_cli.web.db.database import get_db
from kimi_cli.web.user_auth import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Response / request models
# ---------------------------------------------------------------------------


class UserDetail(BaseModel):
    id: str
    username: str
    role: str
    is_active: bool
    created_at: float
    session_count: int


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"


class UpdateUserRequest(BaseModel):
    password: str | None = None
    role: str | None = None
    is_active: bool | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_sessions(db: Any, user_id: str) -> int:
    """Return the number of active (non-expired) sessions for a user."""
    import time

    row = db.execute(
        "SELECT COUNT(*) FROM user_sessions WHERE user_id = ? AND expires_at > ?",
        (user_id, time.time()),
    ).fetchone()
    return int(row[0]) if row else 0


def _user_to_detail(db: Any, user: dict[str, Any]) -> UserDetail:
    return UserDetail(
        id=user["id"],
        username=user["username"],
        role=user["role"],
        is_active=bool(user["is_active"]),
        created_at=user["created_at"],
        session_count=_count_sessions(db, user["id"]),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/users", summary="List all users (admin only)")
async def get_users(
    admin: dict[str, Any] = Depends(require_admin),
) -> list[UserDetail]:
    """Return all users with their active session counts."""
    with get_db() as db:
        users = list_users(db)
        return [_user_to_detail(db, u) for u in users]


@router.post("/users", summary="Create a new user (admin only)", status_code=201)
async def create_user_endpoint(
    body: CreateUserRequest,
    admin: dict[str, Any] = Depends(require_admin),
) -> UserDetail:
    """Create a new user account."""
    import sqlite3

    if body.role not in {"user", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="role must be 'user' or 'admin'",
        )
    try:
        with get_db() as db:
            user = create_user(db, body.username, body.password, body.role)
            return _user_to_detail(db, user)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{body.username}' already exists",
        ) from exc


@router.patch("/users/{user_id}", summary="Update a user (admin only)")
async def update_user_endpoint(
    user_id: str,
    body: UpdateUserRequest,
    admin: dict[str, Any] = Depends(require_admin),
) -> UserDetail:
    """Update role, password, or active status of a user."""
    if body.role is not None and body.role not in {"user", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="role must be 'user' or 'admin'",
        )

    kwargs: dict[str, Any] = {}
    if body.password is not None:
        kwargs["password"] = body.password
    if body.role is not None:
        kwargs["role"] = body.role
    if body.is_active is not None:
        kwargs["is_active"] = body.is_active

    with get_db() as db:
        user = update_user(db, user_id, **kwargs)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        return _user_to_detail(db, user)


@router.delete("/users/{user_id}", summary="Delete a user (admin only)", status_code=204)
async def delete_user_endpoint(
    user_id: str,
    admin: dict[str, Any] = Depends(require_admin),
) -> None:
    """Delete a user.  An admin cannot delete their own account."""
    if user_id == admin["id"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )
    with get_db() as db:
        deleted = delete_user(db, user_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )


__all__ = ["router"]
