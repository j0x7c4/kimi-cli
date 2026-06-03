"""PgKimoStorage unit tests (no live pg).

Covers the parts that don't need a real database:

- owner_id namespace parsing (``hechun-<bigint>`` / ``webui-<uuid>`` / unknown)
- ORM build_step (table names + column names land on the V30 schema)
- Spec §5.5 fallback: write ops swallow exceptions and log

A live-pg round-trip suite is intentionally NOT included here — it requires
either ``pytest-postgresql`` or a hechun-backend Flyway-bootstrapped pg
fixture which is out of scope for task ②. Marked as follow-up for task ⑥/⑦/⑧.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from kimi_cli.memory.entry import MemoryEntry
from kimi_cli.session_state import SessionState


def _make_storage_with_fake_session(rows_sink: list) -> "object":
    """Return a PgKimoStorage instance whose ``_Session()`` produces a fake
    SQLAlchemy session that records every ``.add(row)`` call into rows_sink.
    """
    from kimi_cli.storage.pg_storage import PgKimoStorage

    # Patch create_engine + sessionmaker so __init__ doesn't actually open a connection
    with (
        patch("sqlalchemy.create_engine") as fake_engine,
        patch("sqlalchemy.orm.sessionmaker") as fake_sm,
    ):
        fake_engine.return_value = MagicMock(name="engine")

        class _FakeSession:
            def __init__(self):
                self._txn_active = False

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def begin(self):
                return self

            def add(self, row):
                rows_sink.append(row)

            def get(self, _cls, _pk):
                return None

            def query(self, _cls):
                return self

            def filter(self, *_):
                return self

            def order_by(self, *_):
                return self

            def limit(self, _n):
                return self

            def all(self):
                return []

            def delete(self, _row):
                pass

        fake_sm.return_value = _FakeSession
        return PgKimoStorage(db_url="postgresql://fake@localhost/test", pool_size=1)


class TestOwnerIdNamespace:
    def test_hechun_prefix_parses_user_id(self):
        rows: list = []
        s = _make_storage_with_fake_session(rows)
        entry = MemoryEntry(kind="user", scope="persistent", content="hi")
        s.append_user_memory("hechun-42", entry)
        assert len(rows) == 1
        assert rows[0].user_id == 42
        assert rows[0].owner_id == "hechun-42"

    def test_webui_prefix_keeps_user_id_null(self):
        rows: list = []
        s = _make_storage_with_fake_session(rows)
        entry = MemoryEntry(kind="user", scope="persistent", content="hi")
        s.append_user_memory("webui-deadbeef", entry)
        assert len(rows) == 1
        assert rows[0].user_id is None
        assert rows[0].owner_id == "webui-deadbeef"

    def test_unknown_prefix_skips_append(self):
        rows: list = []
        s = _make_storage_with_fake_session(rows)
        entry = MemoryEntry(kind="user", scope="persistent", content="hi")
        s.append_user_memory("guest-anonymous", entry)
        assert rows == []

    def test_malformed_hechun_user_id_skips(self):
        rows: list = []
        s = _make_storage_with_fake_session(rows)
        entry = MemoryEntry(kind="user", scope="persistent", content="hi")
        s.append_user_memory("hechun-NOT_A_NUMBER", entry)
        assert rows == []


class TestOrmShape:
    def test_table_names_match_v30(self):
        rows: list = []
        s = _make_storage_with_fake_session(rows)
        assert s._KimoSessionStateRow.__tablename__ == "kimo_session_state"
        assert s._AiUserMemoryRow.__tablename__ == "ai_user_memory"

    def test_ai_user_memory_metadata_column_quoted(self):
        """``metadata`` collides with SQLAlchemy's Base.metadata; the column must
        be bound under a distinct Python attribute (``entry_metadata``) while
        the on-disk column stays ``metadata``."""
        rows: list = []
        s = _make_storage_with_fake_session(rows)
        cols = {c.name for c in s._AiUserMemoryRow.__table__.columns}
        assert "metadata" in cols
        # Python attribute is renamed to avoid shadowing
        attr_names = {c.key for c in s._AiUserMemoryRow.__table__.columns}
        assert "entry_metadata" in attr_names
        assert "metadata" not in attr_names


class TestSpec55Fallback:
    def test_save_session_state_swallows_engine_exception(
        self, monkeypatch: pytest.MonkeyPatch, caplog
    ):
        """A DB-down save_session_state must not crash the caller."""
        rows: list = []
        s = _make_storage_with_fake_session(rows)

        # Force _Session() to blow up to simulate a dead engine
        def boom(*_a, **_kw):
            raise RuntimeError("pg down")

        s._Session = boom  # type: ignore[method-assign]
        # Should not raise
        s.save_session_state(uuid4(), "hechun-1", SessionState())

    def test_append_user_memory_swallows_engine_exception(self):
        rows: list = []
        s = _make_storage_with_fake_session(rows)

        def boom(*_a, **_kw):
            raise RuntimeError("pg down")

        s._Session = boom  # type: ignore[method-assign]
        s.append_user_memory(
            "hechun-1", MemoryEntry(kind="user", scope="persistent", content="x")
        )  # no raise


class TestOwnerIdNoneSentinel:
    def test_save_session_state_with_none_owner_uses_sentinel(self):
        rows: list = []
        s = _make_storage_with_fake_session(rows)
        sid = uuid4()
        s.save_session_state(sid, owner_id=None, state=SessionState())
        assert len(rows) == 1
        assert rows[0].owner_id == "__anonymous__"
        assert rows[0].kimo_session_id == sid
