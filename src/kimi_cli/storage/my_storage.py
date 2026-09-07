"""MySQL-backed implementation of :class:`KimoStorage` (hechun fork; spec §8.3/§8.4).

# hechun-fork-cci

Replaces ``PgKimoStorage`` for the MySQL 8.0 era (backend / ops-web migrated PG →
MySQL, design ``2026-06-11-pg-to-mysql-migration-design.md``). Selected when
``KIMI_STORAGE_BACKEND=mysql`` + ``KIMO_DB_URL=mysql+pymysql://...``. Under the CCI
spawner there is NO file fallback (Pod has no persistent volume — spec §8.3).

Schema is owned by backend Flyway (``V1__init_mysql.sql`` §13 baseline +
``V13__ai_user_memory_autoincrement.sql`` which补 ``ai_user_memory.id`` 的
AUTO_INCREMENT — 之前缺失导致本 storage 不传 id 时 MySQL 落 0、第二条起撞主键)
— kimo never creates or migrates tables, only reads/writes. Authoritative shape
(verified against the backend SQL, lines 712-767):

    kimo_session_state(
        kimo_session_id CHAR(36)     PRIMARY KEY,   -- UUID, no Snowflake
        owner_id        VARCHAR(128) NOT NULL,
        state           JSON         NOT NULL,       -- native MySQL JSON, no ::jsonb
        created_at      DATETIME(6)  NOT NULL,       -- UTC
        updated_at      DATETIME(6)  NOT NULL)       -- UTC; no DB ON UPDATE → set in app

    ai_user_memory(
        id                     BIGINT PRIMARY KEY AUTO_INCREMENT,  -- DB 自增（不传 id）
        user_id                BIGINT,               -- nullable (V30)
        owner_id               VARCHAR(128),
        source_kimo_session_id CHAR(36),             -- nullable
        kind                   VARCHAR(32) NOT NULL,
        content                TEXT        NOT NULL,
        metadata               JSON,                 -- native JSON, column name "metadata"
        kimo_line_no           BIGINT,
        created_at             DATETIME(6) NOT NULL)

§8.4 踩坑 checklist (all enforced below):
  #1/#6 time_zone='+00:00' on every connection + store/read UTC naive datetimes
  #3    explicit sql_mode aligned with backend
  #4    JSON columns: pass JSON string directly, read with json.loads, NO cast
  #5    table names all lowercase
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from kimi_cli.memory.entry import MemoryEntry
from kimi_cli.session_state import SessionState
from kimi_cli.utils.logging import logger

if TYPE_CHECKING:
    from sqlalchemy.engine import URL, Engine

_ANONYMOUS = "__anonymous__"

# §8.4 #1/#6: session time zone — MUST match backend exactly because kimo and
# backend write the SAME ``ai_user_memory`` rows in the SAME RDS / hechun DB.
# backend datasource sets ``connectionTimeZone=Asia/Shanghai`` (+08:00) and the
# RDS server time_zone is +08:00, so kimo aligns to +08:00 (NOT UTC). A mismatch
# here is exactly the 8h-drift bug we are avoiding.
_SESSION_TIME_ZONE = "+08:00"

# §8.4 #3: sql_mode is intentionally NOT forced. backend does not set sql_mode
# (uses the RDS default); kimo follows suit so shared-table writes behave
# identically. Pinning a different mode here would diverge from backend.


def _build_orm() -> tuple[Any, type, type]:
    """Build the declarative base + the two ORM row types (MySQL dialect).

    Done in a function so importing this module without sqlalchemy/pymysql
    installed surfaces a clear error at construction, not at import (mirrors
    pg_storage._build_orm).
    """
    from sqlalchemy import (  # noqa: PLC0415
        CHAR,
        BigInteger,
        Column,
        DateTime,
        String,
        Text,
    )
    from sqlalchemy.dialects.mysql import JSON as MySQLJSON  # noqa: PLC0415
    from sqlalchemy.orm import declarative_base  # noqa: PLC0415

    Base = declarative_base()

    class _KimoSessionStateRow(Base):  # type: ignore[misc, valid-type]
        __tablename__ = "kimo_session_state"  # §8.4 #5: lowercase
        # §8.4: UUID is CHAR(36) in MySQL (no native UUID type). Stored as the
        # canonical hyphenated string.
        kimo_session_id = Column(CHAR(36), primary_key=True)
        owner_id = Column(String(128), nullable=False)  # backend: NOT NULL
        # §8.4 #4: native MySQL JSON; SQLAlchemy serialises dict→JSON string, no cast.
        state = Column(MySQLJSON, nullable=False)
        # §8.4 #1/#6: naive UTC DATETIME(6). timezone=False ⇒ no tz conversion.
        created_at = Column(DateTime(timezone=False), nullable=False)
        updated_at = Column(DateTime(timezone=False), nullable=False)

    class _AiUserMemoryRow(Base):  # type: ignore[misc, valid-type]
        __tablename__ = "ai_user_memory"
        id = Column(BigInteger, primary_key=True, autoincrement=True)
        user_id = Column(BigInteger, nullable=True)
        source_session_id = Column(BigInteger, nullable=True)
        source_kimo_session_id = Column(CHAR(36), nullable=True)
        owner_id = Column(String(128), nullable=True)
        kind = Column(String(32), nullable=False)
        content = Column(Text, nullable=False)
        # ``metadata`` collides with Base.metadata; bind under entry_metadata,
        # DB column stays "metadata" (same trick as pg_storage).
        entry_metadata = Column(
            "metadata", MySQLJSON, nullable=True, key="entry_metadata"
        )
        kimo_line_no = Column(BigInteger, nullable=True)
        created_at = Column(DateTime(timezone=False), nullable=False)

    return Base, _KimoSessionStateRow, _AiUserMemoryRow


class MyKimoStorage:
    """MySQL-backed storage (hechun fork; spec §8.3/§8.4).

    Write ops swallow exceptions (spec §5.5: memory/state writes must not crash
    the LLM stream). Read ops propagate so callers can decide on fallback.
    """

    def __init__(self, db_url: str | URL, pool_size: int = 5):
        from sqlalchemy import create_engine, event, text  # noqa: PLC0415
        from sqlalchemy.engine import URL as _URL  # noqa: PLC0415
        from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

        # ``db_url`` may be a SQLAlchemy ``URL`` object (built from separate
        # username/password/host components by build_storage — password special
        # chars escaped safely) or a raw connection string (KIMO_DB_URL override).
        # For string input we tack on charset=utf8mb4 if absent (§8.4 template);
        # URL objects already carry the charset query, so leave them untouched.
        if isinstance(db_url, _URL):
            engine_url: str | URL = db_url
        elif "charset=" not in db_url:
            sep = "&" if "?" in db_url else "?"
            engine_url = f"{db_url}{sep}charset=utf8mb4"
        else:
            engine_url = db_url

        self._engine: Engine = create_engine(
            engine_url, pool_size=pool_size, pool_pre_ping=True, future=True
        )

        # §8.4 #1/#6: every new DBAPI connection sets the session time_zone BEFORE
        # any statement runs (connect event ⇒ also applies to pooled reconnects).
        # time_zone is +08:00 to match backend connectionTimeZone=Asia/Shanghai;
        # sql_mode is NOT set (RDS default, same as backend — §8.4 #3).
        @event.listens_for(self._engine, "connect")
        def _set_session_vars(dbapi_conn, _conn_record):  # pyright: ignore[reportUnusedFunction]
            cursor = dbapi_conn.cursor()
            try:
                cursor.execute(f"SET time_zone = '{_SESSION_TIME_ZONE}'")
            finally:
                cursor.close()

        self._text = text
        self._Session = sessionmaker(bind=self._engine, expire_on_commit=False)
        _Base, self._KimoSessionStateRow, self._AiUserMemoryRow = _build_orm()
        # Tables are created by backend Flyway V1__init_mysql.sql §13; we do NOT
        # call Base.metadata.create_all().
        logger.info("[MyKimoStorage] initialized pool_size={n}", n=pool_size)

    @property
    def engine(self) -> Engine:
        """The shared SQLAlchemy engine.

        Exposed so other hechun-fork gateway components that must talk to the
        SAME MySQL (currently the warm-pool store, which reads/writes
        ``kimo_sandbox_pod``) can reuse this connection pool instead of building
        a second one from the same credentials. Schema ownership is unchanged:
        backend Flyway owns every table; nothing here creates or migrates.
        """
        return self._engine

    # ── session state ───

    def load_session_state(self, kimo_session_id: UUID) -> SessionState | None:
        pk = str(kimo_session_id)
        with self._Session() as session:
            row = session.get(self._KimoSessionStateRow, pk)
            if row is None:
                return None
            # §8.4 #4: state is native JSON. PyMySQL/SQLAlchemy JSON type already
            # returns a dict; tolerate a raw string too (json.loads, never cast).
            raw = row.state
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    logger.warning(
                        "[MyKimoStorage] corrupt state JSON sid={sid}", sid=kimo_session_id
                    )
                    return None
            try:
                return SessionState.model_validate(raw)
            except Exception as e:
                logger.warning(
                    "[MyKimoStorage] state validate failed sid={sid}: {err}",
                    sid=kimo_session_id,
                    err=e,
                )
                return None

    def save_session_state(
        self, kimo_session_id: UUID, owner_id: str | None, state: SessionState
    ) -> None:
        pk = str(kimo_session_id)
        if not owner_id:
            logger.warning(
                "[MyKimoStorage] save_session_state owner_id empty sid={sid}; sentinel",
                sid=kimo_session_id,
            )
            owner_id = _ANONYMOUS
        now = self._utc_naive()
        payload = state.model_dump(mode="json")
        try:
            with self._Session() as session, session.begin():
                row = session.get(self._KimoSessionStateRow, pk)
                if row is None:
                    session.add(
                        self._KimoSessionStateRow(
                            kimo_session_id=pk,
                            owner_id=owner_id,
                            state=payload,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                else:
                    row.owner_id = owner_id
                    row.state = payload
                    # §8.4: no DB-level ON UPDATE → set updated_at in app.
                    row.updated_at = now
        except Exception as e:
            logger.error(
                "[MyKimoStorage] save_session_state failed sid={sid}: {err}",
                sid=kimo_session_id,
                err=e,
            )

    def delete_session_state(self, kimo_session_id: UUID) -> None:
        pk = str(kimo_session_id)
        try:
            with self._Session() as session, session.begin():
                row = session.get(self._KimoSessionStateRow, pk)
                if row is not None:
                    session.delete(row)
        except Exception as e:
            logger.error(
                "[MyKimoStorage] delete_session_state failed sid={sid}: {err}",
                sid=kimo_session_id,
                err=e,
            )

    # ── user memory ───

    def append_user_memory(self, owner_id: str, entry: MemoryEntry) -> None:
        """Append a memory entry.

        ``owner_id`` namespace (spec §2.4.2.D, same as pg backend):
          - ``hechun-<bigint>`` → parsed into ``user_id`` BIGINT
          - ``webui-<uuid>``    → ``user_id`` NULL
          - other               → swallow + log
        """
        user_id_bigint: int | None = None
        if owner_id.startswith("hechun-"):
            try:
                user_id_bigint = int(owner_id[len("hechun-") :])
            except ValueError:
                logger.warning(
                    "[MyKimoStorage] bad hechun owner_id={oid}, skip", oid=owner_id
                )
                return
        elif owner_id.startswith("webui-"):
            pass
        else:
            logger.warning(
                "[MyKimoStorage] unknown owner_id namespace {oid}, skip", oid=owner_id
            )
            return

        src_kimo = getattr(entry, "source_kimo_session_id", None)
        if isinstance(src_kimo, UUID):
            src_kimo = str(src_kimo)
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
                        entry_metadata=meta,  # §8.4 #4: native JSON, no cast
                        kimo_line_no=None,
                        created_at=self._utc_naive(),
                    )
                )
        except Exception as e:
            logger.error(
                "[MyKimoStorage] append_user_memory failed owner_id={oid}: {err}",
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
            meta = r.entry_metadata
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except ValueError:
                    meta = {}
            meta = meta or {}
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
                    "[MyKimoStorage] skip malformed memory row id={rid}: {err}",
                    rid=getattr(r, "id", "?"),
                    err=e,
                )
        return result

    # ── internals ───

    @staticmethod
    def _utc_naive() -> datetime:
        """UTC wall-clock as a naive datetime (§8.4 #1/#6).

        Session time_zone is '+00:00', so storing a naive UTC value round-trips
        without an 8h drift. We strip tzinfo to keep DATETIME(6) (no tz) happy.
        """
        return datetime.now(tz=UTC).replace(tzinfo=None)


__all__ = ["MyKimoStorage"]
