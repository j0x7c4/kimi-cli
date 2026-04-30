"""Admin API endpoints for user management."""

from __future__ import annotations

import os
from pathlib import Path
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


# ---------------------------------------------------------------------------
# Knowledge base index editor
# ---------------------------------------------------------------------------


class KnowledgeIndex(BaseModel):
    path: str
    content: str
    exists: bool


class WriteKnowledgeIndexRequest(BaseModel):
    content: str


def _admin_knowledge_dir() -> Path:
    """Resolve the shared knowledge base directory the admin panel manages.

    Mirrors the work_dir defaulting used by session creation: prefer
    ``KIMI_DEFAULT_WORK_DIR``, falling back to the user's home directory.
    """
    default_dir = os.environ.get("KIMI_DEFAULT_WORK_DIR")
    base = Path(default_dir).expanduser().resolve() if default_dir else Path.home()
    return base / ".kimi" / "memory" / "knowledge"


@router.get("/knowledge/index", summary="Read the shared knowledge index (admin only)")
async def get_knowledge_index(
    admin: dict[str, Any] = Depends(require_admin),
) -> KnowledgeIndex:
    """Return ``index.md`` from the default work_dir's knowledge base."""
    path = _admin_knowledge_dir() / "index.md"
    if not path.is_file():
        return KnowledgeIndex(path=str(path), content="", exists=False)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read {path}: {exc}",
        ) from exc
    return KnowledgeIndex(path=str(path), content=content, exists=True)


@router.put("/knowledge/index", summary="Write the shared knowledge index (admin only)")
async def put_knowledge_index(
    body: WriteKnowledgeIndexRequest,
    admin: dict[str, Any] = Depends(require_admin),
) -> KnowledgeIndex:
    """Overwrite ``index.md`` in the default work_dir's knowledge base."""
    kb_dir = _admin_knowledge_dir()
    try:
        kb_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create {kb_dir}: {exc}",
        ) from exc
    path = kb_dir / "index.md"
    try:
        path.write_text(body.content, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to write {path}: {exc}",
        ) from exc
    return KnowledgeIndex(path=str(path), content=body.content, exists=True)


__all__ = ["router"]
