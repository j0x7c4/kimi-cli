"""Pluggable storage backend for kimo session state + user memory (spec §2.4.2.C).

Selected at startup via env ``KIMI_STORAGE_BACKEND`` ∈ {``file`` (default),
``postgres``, ``mysql``}:

- ``file``  — upstream-compatible filesystem IO; wraps existing
  :mod:`kimi_cli.session_state` + :mod:`kimi_cli.memory.storage` helpers
- ``postgres`` — hechun fork (legacy PG era); writes to hechun pg
  (``kimo_session_state`` + ``ai_user_memory`` tables, Flyway V30).
- ``mysql`` — hechun fork (CCI / MySQL 8.0 era; spec §8.3); writes to the same
  logical tables created by backend Flyway ``V1__init_mysql.sql`` §13.
  # hechun-fork-cci

Under the CCI spawner (``KIMI_SPAWNER_BACKEND=cci``) the Pod has no persistent
volume, so ``file`` is not a valid fallback — :func:`build_storage` refuses to
start with file backend in that mode (spec §8.3).

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

    Reads ``KIMI_STORAGE_BACKEND`` env (default ``file``).

    - ``postgres`` — requires ``KIMO_DB_URL``.
    - ``mysql`` — built from separate ``MYSQL_HOST`` / ``MYSQL_USER`` /
      ``MYSQL_PASSWORD`` (+ optional ``MYSQL_PORT`` 3306 / ``MYSQL_DB`` hechun)
      via ``sqlalchemy.URL.create`` (password special chars escaped safely), or
      from a raw ``KIMO_DB_URL`` override if the components are absent.

    ``KIMO_DB_POOL_SIZE`` is optional for both DB backends (default 5).
    """
    backend = (os.environ.get("KIMI_STORAGE_BACKEND") or "file").strip().lower()
    spawner_backend = (os.environ.get("KIMI_SPAWNER_BACKEND") or "docker").strip().lower()

    # hechun-fork-cci (方案 B): the CCI worker cannot reach RDS, so persistent
    # user memory is delegated to the gateway over the wire. When the gateway
    # sets ``KIMO_MEMORY_VIA_GATEWAY`` for a Pod, the worker's storage is a
    # RemoteKimoStorage proxy (no DB connection). Docker/SIT (worker reaches the
    # DB directly) and file/dev mode never set this flag → unchanged path.
    via_gateway = (os.environ.get("KIMO_MEMORY_VIA_GATEWAY") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if backend in ("postgres", "mysql") and via_gateway:
        from kimi_cli.storage.remote_storage import RemoteKimoStorage

        return RemoteKimoStorage()

    if backend in ("postgres", "mysql"):
        pool_size_str = os.environ.get("KIMO_DB_POOL_SIZE", "5")
        try:
            pool_size = int(pool_size_str)
        except ValueError as e:
            raise RuntimeError(f"KIMO_DB_POOL_SIZE not int: {pool_size_str!r}") from e

        if backend == "postgres":
            db_url = os.environ.get("KIMO_DB_URL")
            if not db_url:
                raise RuntimeError(
                    "KIMI_STORAGE_BACKEND=postgres but KIMO_DB_URL is not set"
                )
            from kimi_cli.storage.pg_storage import PgKimoStorage

            return PgKimoStorage(db_url, pool_size=pool_size)

        # mysql (hechun-fork-cci; spec §8.3). kimo + backend share the SAME RDS /
        # hechun DB, so connection credentials are taken from separate MYSQL_*
        # components and assembled via sqlalchemy.URL.create — which escapes the
        # password internally, so an ``@`` / ``:`` / ``/`` in the password can't
        # corrupt the host/port parsing. KIMO_DB_URL stays as an optional raw
        # override (e.g. for an already-encoded DSN).
        from kimi_cli.storage.my_storage import MyKimoStorage

        db_url_override = os.environ.get("KIMO_DB_URL")
        host = os.environ.get("MYSQL_HOST")
        user = os.environ.get("MYSQL_USER")
        password = os.environ.get("MYSQL_PASSWORD")
        if host and user and password:
            from sqlalchemy import URL  # noqa: PLC0415

            url = URL.create(
                "mysql+pymysql",
                username=user,
                password=password,
                host=host,
                port=int(os.environ.get("MYSQL_PORT", "3306")),
                database=os.environ.get("MYSQL_DB", "hechun"),
                query={"charset": "utf8mb4"},
            )
            return MyKimoStorage(url, pool_size=pool_size)
        if db_url_override:
            return MyKimoStorage(db_url_override, pool_size=pool_size)
        raise RuntimeError(
            "KIMI_STORAGE_BACKEND=mysql requires either MYSQL_HOST + MYSQL_USER + "
            "MYSQL_PASSWORD (preferred; password special chars safe) or a non-empty "
            "KIMO_DB_URL override"
        )

    # spec §8.3: CCI mode has no persistent volume → file backend is invalid.
    if spawner_backend == "cci":
        raise RuntimeError(
            "KIMI_SPAWNER_BACKEND=cci requires a DB storage backend "
            "(KIMI_STORAGE_BACKEND=mysql); 'file' has no persistent volume on CCI Pods"
        )

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
