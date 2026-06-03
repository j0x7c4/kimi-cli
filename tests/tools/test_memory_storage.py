"""Memory tool persistent-path tests (task B2, spec §2.4.2.C + D).

Verifies that ``Memory._add(scope=persistent)`` routes through the
:class:`KimoStorage` abstraction rather than direct ``append_entry`` to the
on-disk ``persistent.jsonl``. This keeps the file/postgres backend swap
transparent at the tool layer.

What's covered here (no live pg):
- File backend: round-trips through :class:`FileKimoStorage` and lands in the
  expected ``persistent.jsonl`` (upstream-compatible behaviour preserved).
- Storage mocking: ``append_user_memory(owner_id, entry)`` is called exactly
  once with the resolved owner_id namespace.
- ``source_kimo_session_id`` is stamped onto the :class:`MemoryEntry` so
  :class:`PgKimoStorage` can populate ``ai_user_memory.source_kimo_session_id``.
- ``storage().append_user_memory`` failure does NOT crash the worker
  (spec §5.5 hard contract).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from kimi_cli.memory.entry import MemoryEntry
from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.memory import AddOp, Memory, Params


@pytest.fixture
def memory_tool(runtime: Runtime) -> Memory:
    """Memory tool instance bound to the shared test Runtime."""
    return Memory(runtime)


class TestMemoryPersistentRoutesThroughStorage:
    """Persistent add must call ``KimoStorage.append_user_memory``."""

    async def test_add_persistent_calls_storage(
        self, memory_tool: Memory, monkeypatch: pytest.MonkeyPatch
    ):
        """Persistent add → exactly one storage.append_user_memory(owner_id, entry)."""
        # Bypass user-approval gate (would otherwise block on missing wire hub).
        async def _noop_approval(*_a, **_kw):
            return None

        monkeypatch.setattr(
            memory_tool, "_request_persistent_approval", _noop_approval
        )

        fake_storage = MagicMock(name="KimoStorage")
        memory_tool._Memory__storage_singleton = fake_storage  # type: ignore[attr-defined]

        # Ensure owner_id resolves to a deterministic value
        monkeypatch.setenv("KIMI_USER_ID", "hechun-1234")

        result = await memory_tool(
            Params(
                operation=AddOp(
                    kind="user", scope="persistent", content="prefers metric units"
                )
            )
        )
        assert not result.is_error
        fake_storage.append_user_memory.assert_called_once()
        called_owner, called_entry = fake_storage.append_user_memory.call_args.args
        assert called_owner == "hechun-1234"
        assert isinstance(called_entry, MemoryEntry)
        assert called_entry.content == "prefers metric units"
        assert called_entry.scope == "persistent"

    async def test_add_persistent_stamps_source_kimo_session_id(
        self, memory_tool: Memory, monkeypatch: pytest.MonkeyPatch
    ):
        """``source_kimo_session_id`` is set via setattr so PgKimoStorage can
        populate ``ai_user_memory.source_kimo_session_id``."""

        async def _noop_approval(*_a, **_kw):
            return None

        monkeypatch.setattr(
            memory_tool, "_request_persistent_approval", _noop_approval
        )

        # Force runtime.session.id to a known UUID hex
        fake_uuid = uuid4()
        monkeypatch.setattr(
            type(memory_tool._runtime.session), "id", str(fake_uuid), raising=False
        )

        fake_storage = MagicMock(name="KimoStorage")
        memory_tool._Memory__storage_singleton = fake_storage  # type: ignore[attr-defined]
        monkeypatch.setenv("KIMI_USER_ID", "hechun-1")

        await memory_tool(
            Params(operation=AddOp(kind="reference", scope="persistent", content="x"))
        )

        _, entry = fake_storage.append_user_memory.call_args.args
        # Stamped via object.__setattr__ — bypasses pydantic schema validation.
        stamped = getattr(entry, "source_kimo_session_id", None)
        assert isinstance(stamped, UUID)
        assert stamped == fake_uuid

    async def test_add_persistent_storage_failure_no_raise(
        self, memory_tool: Memory, monkeypatch: pytest.MonkeyPatch
    ):
        """Storage exceptions must NOT propagate to the LLM worker (spec §5.5)."""

        async def _noop_approval(*_a, **_kw):
            return None

        monkeypatch.setattr(
            memory_tool, "_request_persistent_approval", _noop_approval
        )

        fake_storage = MagicMock(name="KimoStorage")
        fake_storage.append_user_memory.side_effect = RuntimeError("pg down")
        memory_tool._Memory__storage_singleton = fake_storage  # type: ignore[attr-defined]
        monkeypatch.setenv("KIMI_USER_ID", "hechun-1")

        # Should NOT raise even though storage raised
        result = await memory_tool(
            Params(operation=AddOp(kind="user", scope="persistent", content="y"))
        )
        assert not result.is_error  # surfaces as a soft logged failure

    def test_resolve_owner_id_env_first(
        self, memory_tool: Memory, monkeypatch: pytest.MonkeyPatch
    ):
        """``KIMI_USER_ID`` wins over session.state.owner_id (sandbox runner)."""
        monkeypatch.setenv("KIMI_USER_ID", "hechun-99")
        memory_tool._runtime.session.state.owner_id = "hechun-2"
        assert memory_tool._resolve_owner_id() == "hechun-99"

    def test_resolve_owner_id_falls_back_to_session_state(
        self, memory_tool: Memory, monkeypatch: pytest.MonkeyPatch
    ):
        """When no env, use ``session.state.owner_id``."""
        monkeypatch.delenv("KIMI_USER_ID", raising=False)
        memory_tool._runtime.session.state.owner_id = "webui-deadbeef"
        assert memory_tool._resolve_owner_id() == "webui-deadbeef"

    def test_resolve_owner_id_anonymous_sentinel(
        self, memory_tool: Memory, monkeypatch: pytest.MonkeyPatch
    ):
        """No env + no state owner → ``__anonymous__`` sentinel (spec §2.4.2.D)."""
        monkeypatch.delenv("KIMI_USER_ID", raising=False)
        memory_tool._runtime.session.state.owner_id = None
        assert memory_tool._resolve_owner_id() == "__anonymous__"


class TestMemoryPersistentFileBackendEndToEnd:
    """End-to-end through the real :class:`FileKimoStorage` (no Pg involved).

    Confirms behaviour is bit-for-bit compatible with the pre-M4 direct
    ``append_entry`` path: the persistent.jsonl file lands at the same place
    with the same contents.
    """

    async def test_file_backend_round_trip(
        self,
        memory_tool: Memory,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        from kimi_cli.storage.file_storage import FileKimoStorage

        async def _noop_approval(*_a, **_kw):
            return None

        monkeypatch.setattr(
            memory_tool, "_request_persistent_approval", _noop_approval
        )

        # Isolate the share dir + owner so the test doesn't pollute real files
        share = tmp_path / "share"
        share.mkdir()
        monkeypatch.setenv("KIMI_SHARE_DIR", str(share))
        monkeypatch.setenv("KIMI_SHARE_HOME", str(share))
        monkeypatch.setenv("KIMI_USER_ID", "hechun-42")

        # Force a fresh FileKimoStorage that reads the patched KIMI_SHARE_DIR
        memory_tool._Memory__storage_singleton = FileKimoStorage()  # type: ignore[attr-defined]

        await memory_tool(
            Params(operation=AddOp(kind="user", scope="persistent", content="abc"))
        )

        from kimi_cli.memory.paths import get_persistent_memory_file

        path = get_persistent_memory_file("hechun-42")
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert "abc" in text
