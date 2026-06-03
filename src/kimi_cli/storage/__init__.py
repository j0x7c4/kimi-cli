"""Pluggable storage backend for kimo session state + user memory (spec §2.4.2.C).

Selected at startup via env ``KIMI_STORAGE_BACKEND`` ∈ {``file`` (default), ``postgres``}:

- ``file``  — upstream-compatible filesystem IO; wraps existing
  :mod:`kimi_cli.session_state` + :mod:`kimi_cli.memory.storage` helpers
- ``postgres`` — hechun fork; writes to hechun pg
  (``kimo_session_state`` + ``ai_user_memory`` tables, Flyway V30).

wire.jsonl + context.jsonl are NOT in this Protocol; they remain on the host
volume (spec §2.4.2.A decision). Only ``state.json`` (session state) and
``persistent.jsonl`` (user memory) flow through this interface.

``build_storage()`` is the single factory consumed by web/app.py lifespan and
(future) sandbox worker bootstrap; downstream task ⑥/⑦/⑧ will switch
session_state.py / archivist.py / sessions.py to call this Protocol instead of
the file helpers directly.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from kimi_cli.memory.entry import MemoryEntry
from kimi_cli.session_state import SessionState

if TYPE_CHECKING:
    # Avoid eager psycopg2 import on file backend; resolved lazily in build_storage.
    pass


class KimoStorage(Protocol):
    """Abstract storage for session state + user memory (spec §2.4.2.C)."""

    # ─── session state ───
    def load_session_state(self, kimo_session_id: UUID) -> SessionState | None: ...
    def save_session_state(
        self, kimo_session_id: UUID, owner_id: str | None, state: SessionState
    ) -> None: ...
    def delete_session_state(self, kimo_session_id: UUID) -> None: ...

    # ─── user memory (append-only) ───
    def append_user_memory(self, owner_id: str, entry: MemoryEntry) -> None: ...
    def list_user_memory(self, owner_id: str, limit: int = 200) -> list[MemoryEntry]: ...


def build_storage() -> KimoStorage:
    """Construct the configured storage backend.

    Reads ``KIMI_STORAGE_BACKEND`` env (default ``file``). When ``postgres``,
    ``KIMO_DB_URL`` is required and ``KIMO_DB_POOL_SIZE`` is optional (default 5).
    """
    backend = (os.environ.get("KIMI_STORAGE_BACKEND") or "file").strip().lower()
    if backend == "postgres":
        from kimi_cli.storage.pg_storage import PgKimoStorage

        db_url = os.environ.get("KIMO_DB_URL")
        if not db_url:
            raise RuntimeError(
                "KIMI_STORAGE_BACKEND=postgres but KIMO_DB_URL is not set"
            )
        pool_size_str = os.environ.get("KIMO_DB_POOL_SIZE", "5")
        try:
            pool_size = int(pool_size_str)
        except ValueError as e:
            raise RuntimeError(f"KIMO_DB_POOL_SIZE not int: {pool_size_str!r}") from e
        return PgKimoStorage(db_url, pool_size=pool_size)

    if backend != "file":
        # Unknown values → fall back to file but log loudly so a typo in
        # deploy config doesn't silently disable the pg backend.
        from kimi_cli.utils.logging import logger

        logger.warning(
            "[storage] unknown KIMI_STORAGE_BACKEND={b!r}; falling back to file backend",
            b=backend,
        )

    # Default: file backend (upstream-compatible)
    from kimi_cli.storage.file_storage import FileKimoStorage

    return FileKimoStorage()


__all__ = ["KimoStorage", "build_storage"]
