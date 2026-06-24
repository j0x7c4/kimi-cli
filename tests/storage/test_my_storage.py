"""MyKimoStorage unit tests (no live MySQL) — spec §8.3/§8.4.

# hechun-fork-cci

Covers the parts that don't need a real database:

- session state read/write hits ``kimo_session_state`` with CHAR(36) UUID PK
- owner_id namespace parsing for ai_user_memory (same as pg backend)
- ORM shape lands on backend Flyway V1__init_mysql.sql §13 (table names lowercase,
  JSON columns native, no ::jsonb cast)
- §8.4 #1/#6: updated_at set by app on save; UTC naive datetimes
- §5.5 fallback: write ops swallow exceptions

A live-MySQL round-trip suite is out of scope (no RDS / no docker MySQL in unit
test env, per agent constraints).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

from kimi_cli.memory.entry import MemoryEntry
from kimi_cli.session_state import SessionState


def _make_storage_with_fake_session(rows_sink: list, get_returns=None):
    """Build a MyKimoStorage whose _Session() yields a fake recording session."""
    from kimi_cli.storage.my_storage import MyKimoStorage

    with (
        patch("sqlalchemy.create_engine") as fake_engine,
        patch("sqlalchemy.orm.sessionmaker") as fake_sm,
        patch("sqlalchemy.event.listens_for") as fake_listen,
    ):
        fake_engine.return_value = MagicMock(name="engine")
        # event.listens_for is used as a decorator; return an identity decorator.
        fake_listen.return_value = lambda fn: fn

        class _FakeSession:
            def __init__(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def begin(self):
                return self

            def add(self, row):
                rows_sink.append(row)

            def get(self, _cls, _pk):
                return get_returns

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
                rows_sink.append(("DELETE", _row))

        fake_sm.return_value = _FakeSession
        return MyKimoStorage(db_url="mysql+pymysql://fake@localhost/hechun", pool_size=1)


class TestOrmShape:
    def test_table_names_lowercase_match_backend(self):
        s = _make_storage_with_fake_session([])
        assert s._KimoSessionStateRow.__tablename__ == "kimo_session_state"
        assert s._AiUserMemoryRow.__tablename__ == "ai_user_memory"

    def test_session_state_uuid_is_char36(self):
        s = _make_storage_with_fake_session([])
        col = s._KimoSessionStateRow.__table__.columns["kimo_session_id"]
        # CHAR(36) in MySQL — length 36, not a native UUID type.
        assert col.type.length == 36
        assert col.primary_key

    def test_owner_id_not_null_varchar_128(self):
        s = _make_storage_with_fake_session([])
        col = s._KimoSessionStateRow.__table__.columns["owner_id"]
        assert col.nullable is False
        assert col.type.length == 128

    def test_metadata_column_bound_under_entry_metadata(self):
        s = _make_storage_with_fake_session([])
        cols = {c.name for c in s._AiUserMemoryRow.__table__.columns}
        attrs = {c.key for c in s._AiUserMemoryRow.__table__.columns}
        assert "metadata" in cols
        assert "entry_metadata" in attrs
        assert "metadata" not in attrs


class TestSessionState:
    def test_save_new_session_state_inserts_with_str_pk_and_utc(self):
        rows: list = []
        s = _make_storage_with_fake_session(rows, get_returns=None)
        sid = uuid4()
        s.save_session_state(sid, "hechun-7", SessionState())
        assert len(rows) == 1
        row = rows[0]
        # PK stored as the canonical hyphenated UUID string (CHAR(36)).
        assert row.kimo_session_id == str(sid)
        assert row.owner_id == "hechun-7"
        # §8.4 #1/#6: created_at / updated_at are naive (no tzinfo) UTC.
        assert row.created_at.tzinfo is None
        assert row.updated_at.tzinfo is None
        assert row.created_at == row.updated_at

    def test_save_none_owner_uses_sentinel(self):
        rows: list = []
        s = _make_storage_with_fake_session(rows, get_returns=None)
        s.save_session_state(uuid4(), None, SessionState())
        assert rows[0].owner_id == "__anonymous__"

    def test_save_existing_updates_updated_at(self):
        existing = MagicMock()
        existing.owner_id = "old"
        s = _make_storage_with_fake_session([], get_returns=existing)
        s.save_session_state(uuid4(), "hechun-1", SessionState())
        # §8.4: no DB ON UPDATE → app sets updated_at on the existing row.
        assert existing.updated_at is not None
        assert existing.owner_id == "hechun-1"

    def test_load_returns_none_when_absent(self):
        s = _make_storage_with_fake_session([], get_returns=None)
        assert s.load_session_state(uuid4()) is None

    def test_load_parses_dict_state(self):
        row = MagicMock()
        row.state = {"version": 1, "owner_id": "hechun-1"}
        s = _make_storage_with_fake_session([], get_returns=row)
        st = s.load_session_state(uuid4())
        assert isinstance(st, SessionState)
        assert st.owner_id == "hechun-1"

    def test_load_parses_json_string_state_no_cast(self):
        """§8.4 #4: tolerate raw JSON string (json.loads), never ::jsonb cast."""
        row = MagicMock()
        row.state = '{"version": 1, "owner_id": "hechun-2"}'
        s = _make_storage_with_fake_session([], get_returns=row)
        st = s.load_session_state(uuid4())
        assert st is not None
        assert st.owner_id == "hechun-2"


class TestOwnerIdNamespace:
    def test_hechun_prefix_parses_user_id(self):
        rows: list = []
        s = _make_storage_with_fake_session(rows)
        s.append_user_memory("hechun-42", MemoryEntry(kind="user", scope="persistent", content="hi"))
        assert rows[0].user_id == 42
        assert rows[0].owner_id == "hechun-42"

    def test_webui_prefix_keeps_user_id_null(self):
        rows: list = []
        s = _make_storage_with_fake_session(rows)
        s.append_user_memory("webui-abc", MemoryEntry(kind="user", scope="persistent", content="x"))
        assert rows[0].user_id is None

    def test_unknown_prefix_skips(self):
        rows: list = []
        s = _make_storage_with_fake_session(rows)
        s.append_user_memory("guest-x", MemoryEntry(kind="user", scope="persistent", content="x"))
        assert rows == []

    def test_metadata_round_trips_as_dict(self):
        rows: list = []
        s = _make_storage_with_fake_session(rows)
        e = MemoryEntry(kind="user", scope="persistent", content="c")
        s.append_user_memory("hechun-1", e)
        # entry_metadata is a dict (native JSON; no string cast).
        assert isinstance(rows[0].entry_metadata, dict)
        assert rows[0].entry_metadata["entry_id"] == e.id


class TestSpec55Fallback:
    def test_save_swallows_engine_exception(self):
        s = _make_storage_with_fake_session([])

        def boom(*_a, **_kw):
            raise RuntimeError("mysql down")

        s._Session = boom  # type: ignore[method-assign]
        s.save_session_state(uuid4(), "hechun-1", SessionState())  # no raise

    def test_append_swallows_engine_exception(self):
        s = _make_storage_with_fake_session([])

        def boom(*_a, **_kw):
            raise RuntimeError("mysql down")

        s._Session = boom  # type: ignore[method-assign]
        s.append_user_memory("hechun-1", MemoryEntry(kind="user", scope="persistent", content="x"))


class TestUtcHelper:
    def test_utc_naive_is_naive(self):
        from kimi_cli.storage.my_storage import MyKimoStorage

        dt = MyKimoStorage._utc_naive()
        assert dt.tzinfo is None


def _capture_connect_listener(db_url):
    """Construct MyKimoStorage and capture the @event.listens_for(connect) fn.

    Returns ``(captured_engine_url, listener_fn)`` so tests can both inspect the
    URL handed to create_engine and run the per-connection init against a fake
    cursor to assert which SET statements execute.
    """
    from kimi_cli.storage.my_storage import MyKimoStorage

    captured: dict = {}

    def fake_create_engine(url, **_kw):
        captured["engine_url"] = url
        return MagicMock(name="engine")

    def fake_listens_for(_engine, _event):
        def deco(fn):
            captured["listener"] = fn
            return fn

        return deco

    with (
        patch("sqlalchemy.create_engine", side_effect=fake_create_engine),
        patch("sqlalchemy.orm.sessionmaker", return_value=MagicMock()),
        patch("sqlalchemy.event.listens_for", side_effect=fake_listens_for),
    ):
        MyKimoStorage(db_url=db_url, pool_size=1)
    return captured


class _FakeCursor:
    def __init__(self):
        self.executed: list[str] = []

    def execute(self, sql):
        self.executed.append(sql)

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class TestConnectionInit:
    """§8.4 #1/#3/#6 — must match backend application.yml exactly (shared DB)."""

    def test_session_time_zone_constant_is_shanghai_offset(self):
        from kimi_cli.storage import my_storage

        # backend connectionTimeZone=Asia/Shanghai (+08:00); NOT UTC.
        assert my_storage._SESSION_TIME_ZONE == "+08:00"

    def test_no_sql_mode_constant(self):
        from kimi_cli.storage import my_storage

        # sql_mode intentionally not forced (RDS default, like backend).
        assert not hasattr(my_storage, "_SQL_MODE")

    def test_connect_listener_sets_time_zone_0800_only(self):
        cap = _capture_connect_listener("mysql+pymysql://fake@localhost/hechun")
        listener = cap["listener"]
        cursor = _FakeCursor()
        listener(_FakeConn(cursor), None)
        # Exactly one SET, time_zone = '+08:00'.
        assert cursor.executed == ["SET time_zone = '+08:00'"]
        # No forced sql_mode anywhere.
        assert not any("sql_mode" in s.lower() for s in cursor.executed)


class TestUrlInput:
    def test_url_object_passed_through_without_charset_concat(self):
        from sqlalchemy import URL

        url = URL.create(
            "mysql+pymysql",
            username="hechun",
            password="p@ss:w/rd!",
            host="rds.internal",
            port=4727,
            database="hechun",
            query={"charset": "utf8mb4"},
        )
        cap = _capture_connect_listener(url)
        engine_url = cap["engine_url"]
        # The URL object is handed to create_engine verbatim (not stringified +
        # re-concatenated). Password special chars never touch host parsing.
        assert engine_url is url
        assert engine_url.host == "rds.internal"
        assert engine_url.port == 4727
        assert engine_url.password == "p@ss:w/rd!"
        # charset already present → not duplicated.
        assert engine_url.query.get("charset") == "utf8mb4"

    def test_string_input_gets_charset_appended(self):
        cap = _capture_connect_listener("mysql+pymysql://fake@localhost/hechun")
        engine_url = cap["engine_url"]
        assert isinstance(engine_url, str)
        assert "charset=utf8mb4" in engine_url

    def test_string_input_with_charset_not_doubled(self):
        cap = _capture_connect_listener(
            "mysql+pymysql://fake@localhost/hechun?charset=utf8mb4"
        )
        engine_url = cap["engine_url"]
        assert engine_url.count("charset=") == 1
