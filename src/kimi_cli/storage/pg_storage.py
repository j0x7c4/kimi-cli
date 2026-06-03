"""Postgres-backed implementation of :class:`KimoStorage` (hechun fork; spec §2.4.2.C).

Writes to the hechun pg schema created by Flyway migration V30:

- ``kimo_session_state(kimo_session_id UUID PK, owner_id TEXT NOT NULL,
  state JSONB NOT NULL, created_at, updated_at)``
- ``ai_user_memory`` (V29 + V30 added columns ``owner_id`` and
  ``source_kimo_session_id``; ``user_id`` now nullable)

Selected when ``KIMI_STORAGE_BACKEND=postgres`` + ``KIMO_DB_URL`` is set.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from kimi_cli.memory.entry import MemoryEntry
from kimi_cli.session_state import SessionState
from kimi_cli.utils.logging import logger

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


# Module-level Base + ORM rows kept private; they describe the V30 schema and
# must NOT diverge from the hechun-backend migrations.
def _build_orm() -> tuple[Any, type, type]:
    """Build the SQLAlchemy declarative_base and the two ORM row types.

    Done in a function so that importing this module without psycopg2 / sqlalchemy
    installed still surfaces a clear error at construction time (not at import).
    """
    from sqlalchemy import (  # noqa: PLC0415
        TIMESTAMP,
        BigInteger,
        Column,
        Text,
    )
    from sqlalchemy.dialects.postgresql import JSONB, UUID as SAUUID  # noqa: PLC0415
    from sqlalchemy.orm import declarative_base  # noqa: PLC0415

    Base = declarative_base()

    class _KimoSessionStateRow(Base):  # type: ignore[misc, valid-type]
        __tablename__ = "kimo_session_state"
        kimo_session_id = Column(SAUUID(as_uuid=True), primary_key=True)
        owner_id = Column(Text, nullable=False)
        state = Column(JSONB, nullable=False)
        created_at = Column(TIMESTAMP(timezone=True), nullable=False)
        updated_at = Column(TIMESTAMP(timezone=True), nullable=False)

    class _AiUserMemoryRow(Base):  # type: ignore[misc, valid-type]
        __tablename__ = "ai_user_memory"
        id = Column(BigInteger, primary_key=True, autoincrement=True)
        user_id = Column(BigInteger, nullable=True)
        source_session_id = Column(BigInteger, nullable=True)
        source_kimo_session_id = Column(SAUUID(as_uuid=True), nullable=True)
        owner_id = Column(Text, nullable=True)
        kind = Column(Text, nullable=False)
        content = Column(Text, nullable=False)
        # ``metadata`` collides with SQLAlchemy's Base.metadata; declare under a
        # distinct Python attribute name and bind the column to the SQL name
        # explicitly. ``key=`` forces SQLAlchemy to track the column under
        # ``entry_metadata`` while the DB column stays ``metadata``.
        entry_metadata = Column("metadata", JSONB, nullable=True, key="entry_metadata")
        kimo_line_no = Column(BigInteger, nullable=True)
        created_at = Column(TIMESTAMP(timezone=True), nullable=False)

    return Base, _KimoSessionStateRow, _AiUserMemoryRow


_ANONYMOUS = "__anonymous__"


class PgKimoStorage:
    """Postgres-backed storage (hechun fork).

    All write ops swallow exceptions to honour spec §5.5 fallback: memory
    append/state save errors must not block the LLM stream. They log + return.
    Read ops propagate so callers (sessions.py) can decide whether to fall
    back to the file backend.
    """

    def __init__(self, db_url: str, pool_size: int = 5):
        # Lazy import: psycopg2 + sqlalchemy may be absent on file-only deploys.
        from sqlalchemy import create_engine  # noqa: PLC0415
        from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

        self._engine: Engine = create_engine(
            db_url, pool_size=pool_size, pool_pre_ping=True, future=True
        )
        self._Session = sessionmaker(bind=self._engine, expire_on_commit=False)
        _Base, self._KimoSessionStateRow, self._AiUserMemoryRow = _build_orm()
        # Tables are created by hechun-backend Flyway V30; we intentionally do
        # NOT call Base.metadata.create_all() here.
        logger.info("[PgKimoStorage] initialized pool_size={n}", n=pool_size)

    # ─── session state ───

    def load_session_state(self, kimo_session_id: UUID) -> SessionState | None:
        with self._Session() as session:
            row = session.get(self._KimoSessionStateRow, kimo_session_id)
            if row is None:
                return None
            try:
                return SessionState.model_validate(row.state)
            except Exception as e:
                # Bad JSONB blob — surface as None so callers can fresh-start.
                logger.warning(
                    "[PgKimoStorage] corrupt state JSONB sid={sid}: {err}",
                    sid=kimo_session_id,
                    err=e,
                )
                return None

    def save_session_state(
        self, kimo_session_id: UUID, owner_id: str | None, state: SessionState
    ) -> None:
        if owner_id is None or not owner_id:
            logger.warning(
                "[PgKimoStorage] save_session_state owner_id=None sid={sid}; using sentinel",
                sid=kimo_session_id,
            )
            owner_id = _ANONYMOUS
        now = datetime.now(tz=timezone.utc)
        payload = state.model_dump(mode="json")
        try:
            with self._Session() as session, session.begin():
                row = session.get(self._KimoSessionStateRow, kimo_session_id)
                if row is None:
                    session.add(
                        self._KimoSessionStateRow(
                            kimo_session_id=kimo_session_id,
                            owner_id=owner_id,
                            state=payload,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                else:
                    row.owner_id = owner_id
                    row.state = payload
                    row.updated_at = now
        except Exception as e:
            # Spec §5.5: state save must not crash the LLM stream.
            logger.error(
                "[PgKimoStorage] save_session_state failed sid={sid}: {err}",
                sid=kimo_session_id,
                err=e,
            )

    def delete_session_state(self, kimo_session_id: UUID) -> None:
        try:
            with self._Session() as session, session.begin():
                row = session.get(self._KimoSessionStateRow, kimo_session_id)
                if row is not None:
                    session.delete(row)
        except Exception as e:
            logger.error(
                "[PgKimoStorage] delete_session_state failed sid={sid}: {err}",
                sid=kimo_session_id,
                err=e,
            )

    # ─── user memory ───

    def append_user_memory(self, owner_id: str, entry: MemoryEntry) -> None:
        """Append a memory entry.

        ``owner_id`` namespace convention (spec §2.4.2.D):

        - ``hechun-<bigint>`` → parsed into ``user_id`` (BIGINT FK to app_user)
        - ``webui-<uuid>``    → ``user_id`` left NULL (V30 made user_id nullable)
        - other prefixes     → swallow + log (refuses to write garbage owner)
        """
        user_id_bigint: int | None = None
        if owner_id.startswith("hechun-"):
            try:
                user_id_bigint = int(owner_id[len("hechun-") :])
            except ValueError:
                logger.warning(
                    "[PgKimoStorage] bad hechun owner_id={oid}, skip memory append",
                    oid=owner_id,
                )
                return
        elif owner_id.startswith("webui-"):
            pass  # user_id NULL
        else:
            logger.warning(
                "[PgKimoStorage] unknown owner_id namespace {oid}, skip memory append",
                oid=owner_id,
            )
            return

        # ``source_kimo_session_id`` is set by archivist (task ⑦) — getattr keeps
        # this module compatible with current MemoryEntry shape.
        src_kimo = getattr(entry, "source_kimo_session_id", None)
        # MemoryEntry fields not in V30 columns are tucked into the metadata JSONB
        # (id hex, scope, updated_at) for full round-trip parity with the file
        # backend.
        meta: dict[str, Any] = {
            "entry_id": entry.id,
            "scope": entry.scope,
            "created_at": entry.created_at,
        }
        if entry.updated_at is not None:
            meta["updated_at"] = entry.updated_at
        extra_meta = getattr(entry, "metadata", None)
        if isinstance(extra_meta, dict):
            meta.update(extra_meta)

        try:
            with self._Session() as session, session.begin():
                session.add(
                    self._AiUserMemoryRow(
                        user_id=user_id_bigint,
                        owner_id=owner_id,
                        source_kimo_session_id=src_kimo,
                        kind=entry.kind,
                        content=entry.content,
                        entry_metadata=meta,
                        kimo_line_no=None,
                        created_at=datetime.now(tz=timezone.utc),
                    )
                )
        except Exception as e:
            # Spec §5.5: never block LLM stream on memory write failure.
            logger.error(
                "[PgKimoStorage] append_user_memory failed owner_id={oid}: {err}",
                oid=owner_id,
                err=e,
            )

    def list_user_memory(self, owner_id: str, limit: int = 200) -> list[MemoryEntry]:
        with self._Session() as session:
            rows = (
                session.query(self._AiUserMemoryRow)
                .filter(self._AiUserMemoryRow.owner_id == owner_id)
                .order_by(self._AiUserMemoryRow.created_at.desc())
                .limit(limit)
                .all()
            )
        result: list[MemoryEntry] = []
        for r in rows:
            meta = r.entry_metadata or {}
            # Re-hydrate MemoryEntry; kind must satisfy upstream Literal —
            # validation errors are swallowed (row skipped with a warning).
            try:
                result.append(
                    MemoryEntry(
                        id=meta.get("entry_id") or "",
                        kind=r.kind,
                        scope=meta.get("scope", "persistent"),
                        content=r.content,
                        created_at=float(meta.get("created_at") or 0.0),
                        updated_at=meta.get("updated_at"),
                    )
                )
            except Exception as e:
                logger.warning(
                    "[PgKimoStorage] skip malformed memory row id={rid}: {err}",
                    rid=r.id,
                    err=e,
                )
        return result


__all__ = ["PgKimoStorage"]
