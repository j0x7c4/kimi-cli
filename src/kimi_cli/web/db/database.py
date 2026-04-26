"""SQLite database connection and initialization for user management."""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

# Password hashing: prefer passlib/bcrypt, fallback to hashlib.sha256 with salt
try:
    from passlib.context import CryptContext as _CryptContext

    _pwd_context = _CryptContext(schemes=["bcrypt"], deprecated="auto")

    def _hash_password(plain: str) -> str:
        return _pwd_context.hash(plain)  # type: ignore[no-any-return]

    def _verify_password(plain: str, hashed: str) -> bool:
        return _pwd_context.verify(plain, hashed)  # type: ignore[no-any-return]

except Exception:
    import hashlib
    import os

    def _hash_password(plain: str) -> str:  # type: ignore[misc]
        salt = os.urandom(16).hex()
        digest = hashlib.sha256(f"{salt}:{plain}".encode()).hexdigest()
        return f"sha256${salt}${digest}"

    def _verify_password(plain: str, hashed: str) -> bool:  # type: ignore[misc]
        try:
            _, salt, digest = hashed.split("$", 2)
            expected = hashlib.sha256(f"{salt}:{plain}".encode()).hexdigest()
            import hmac

            return hmac.compare_digest(expected, digest)
        except Exception:
            return False


# Database path — reuse get_share_dir() so KIMI_SHARE_DIR env var is respected.
# In container deployments KIMI_SHARE_DIR should point to a mounted volume,
# which keeps both session data and the user database persistent across restarts.
from kimi_cli.share import get_share_dir as _get_share_dir


def _get_db_path() -> Path:
    share_dir = _get_share_dir()
    return share_dir / "users.db"


_CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
"""

_CREATE_USER_SESSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS user_sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
"""

_CREATE_BRANDING_TABLE = """
CREATE TABLE IF NOT EXISTS branding (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def get_db() -> sqlite3.Connection:
    """Return a SQLite connection to the users database.

    The caller is responsible for closing the connection (or using it as a
    context manager).  Row factory is set to ``sqlite3.Row`` so rows behave
    like dicts.
    """
    db_path = _get_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Initialize the database schema and create the default admin account.

    Safe to call multiple times — uses ``CREATE TABLE IF NOT EXISTS``.  The
    default admin is only created when the ``users`` table is empty.
    """
    with get_db() as conn:
        conn.execute(_CREATE_USERS_TABLE)
        conn.execute(_CREATE_USER_SESSIONS_TABLE)
        conn.execute(_CREATE_BRANDING_TABLE)
        conn.commit()

        # Create default admin only when table is empty
        row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
        if row[0] == 0:
            admin_id = str(uuid.uuid4())
            password_hash = _hash_password("admin123")
            conn.execute(
                """
                INSERT INTO users (id, username, password_hash, role, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (admin_id, "admin", password_hash, "admin", 1, time.time()),
            )
            conn.commit()


__all__ = [
    "get_db",
    "init_db",
]
