"""CRUD operations for user and session management."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
import uuid
from typing import Any

# Password hashing: prefer passlib/bcrypt, fallback to hashlib.sha256 with salt
try:
    from passlib.context import CryptContext as _CryptContext

    _pwd_context = _CryptContext(schemes=["bcrypt"], deprecated="auto")

    def hash_password(plain: str) -> str:
        """Hash a plaintext password."""
        return _pwd_context.hash(plain)  # type: ignore[no-any-return]

    def verify_password(plain: str, hashed: str) -> bool:
        """Verify a plaintext password against its hash."""
        return _pwd_context.verify(plain, hashed)  # type: ignore[no-any-return]

except Exception:

    def hash_password(plain: str) -> str:  # type: ignore[misc]
        """Hash a plaintext password using sha256 with a random salt."""
        salt = os.urandom(16).hex()
        digest = hashlib.sha256(f"{salt}:{plain}".encode()).hexdigest()
        return f"sha256${salt}${digest}"

    def verify_password(plain: str, hashed: str) -> bool:  # type: ignore[misc]
        """Verify a plaintext password against a sha256-salted hash."""
        try:
            _, salt, digest = hashed.split("$", 2)
            expected = hashlib.sha256(f"{salt}:{plain}".encode()).hexdigest()
            return hmac.compare_digest(expected, digest)
        except Exception:
            return False


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------


def get_user_by_username(db: sqlite3.Connection, username: str) -> dict[str, Any] | None:
    """Return user dict for *username*, or ``None`` if not found."""
    row = db.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    return _row_to_dict(row)


def get_user_by_id(db: sqlite3.Connection, user_id: str) -> dict[str, Any] | None:
    """Return user dict for *user_id*, or ``None`` if not found."""
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_dict(row)


def create_user(
    db: sqlite3.Connection,
    username: str,
    password: str,
    role: str = "user",
) -> dict[str, Any]:
    """Create a new user and return the resulting user dict."""
    user_id = str(uuid.uuid4())
    password_hash = hash_password(password)
    now = time.time()
    db.execute(
        """
        INSERT INTO users (id, username, password_hash, role, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, username, password_hash, role, 1, now),
    )
    db.commit()
    user = get_user_by_id(db, user_id)
    assert user is not None
    return user


def update_user(
    db: sqlite3.Connection,
    user_id: str,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """Update user fields.

    Accepted keyword arguments: ``password``, ``role``, ``is_active``.
    Returns the updated user dict, or ``None`` if the user does not exist.
    """
    if not kwargs:
        return get_user_by_id(db, user_id)

    set_clauses: list[str] = []
    params: list[Any] = []

    if "password" in kwargs:
        set_clauses.append("password_hash = ?")
        params.append(hash_password(kwargs["password"]))
    if "role" in kwargs:
        set_clauses.append("role = ?")
        params.append(kwargs["role"])
    if "is_active" in kwargs:
        set_clauses.append("is_active = ?")
        params.append(1 if kwargs["is_active"] else 0)

    if not set_clauses:
        return get_user_by_id(db, user_id)

    params.append(user_id)
    db.execute(
        f"UPDATE users SET {', '.join(set_clauses)} WHERE id = ?",  # noqa: S608
        params,
    )
    db.commit()
    return get_user_by_id(db, user_id)


def delete_user(db: sqlite3.Connection, user_id: str) -> bool:
    """Delete a user and all their sessions.  Returns ``True`` if deleted."""
    # Remove sessions first to avoid orphaned rows
    db.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
    cursor = db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    return cursor.rowcount > 0


def list_users(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all users ordered by creation time."""
    rows = db.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------


def create_user_session(
    db: sqlite3.Connection,
    user_id: str,
    expires_in_seconds: int = 86400,
) -> str:
    """Create a new user session token and persist it.  Returns the token."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    expires_at = now + expires_in_seconds
    db.execute(
        """
        INSERT INTO user_sessions (token, user_id, created_at, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (token, user_id, now, expires_at),
    )
    db.commit()
    return token


def get_user_session(db: sqlite3.Connection, token: str) -> dict[str, Any] | None:
    """Return the user dict associated with *token*, or ``None`` if expired/missing."""
    row = db.execute(
        "SELECT * FROM user_sessions WHERE token = ?", (token,)
    ).fetchone()
    if row is None:
        return None

    session = dict(row)
    if session["expires_at"] < time.time():
        # Session expired — clean up lazily
        db.execute("DELETE FROM user_sessions WHERE token = ?", (token,))
        db.commit()
        return None

    user = get_user_by_id(db, session["user_id"])
    if user is None or not user.get("is_active"):
        return None
    return user


def delete_user_session(db: sqlite3.Connection, token: str) -> None:
    """Delete a user session by token."""
    db.execute("DELETE FROM user_sessions WHERE token = ?", (token,))
    db.commit()


__all__ = [
    "create_user",
    "create_user_session",
    "delete_user",
    "delete_user_session",
    "get_user_by_id",
    "get_user_by_username",
    "get_user_session",
    "hash_password",
    "list_users",
    "update_user",
    "verify_password",
]
