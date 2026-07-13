"""方案 B: the Memory tool no longer exposes a ``list`` operation (hechun-fork).

Some models spin forever calling ``op=list`` on empty persistent memory (SIT: 10+
consecutive lists, never ``add``; even an explicit STOP breaker text was ignored).
``list`` is also redundant — persistent memory is auto-injected into context by the
cross_session_memory dynamic injection (which reads storage directly, NOT this
tool). So ``list`` was removed; the model must ``add`` or answer, and use ids from
the injected memory for ``update`` / ``delete``.

Covered here:
- ``op=list`` is no longer a valid Params operation (schema rejects it).
- ``add`` / ``update`` / ``delete`` still work.
- The recall injection path (storage.alist_user_memory) is independent of the tool
  and unaffected (covered concretely in test_cross_session_memory_recall below).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kimi_cli.soul.agent import Runtime
from kimi_cli.tools import memory as memory_mod
from kimi_cli.tools.memory import AddOp, DeleteOp, Memory, Params, UpdateOp


@pytest.fixture
def memory_tool(runtime: Runtime) -> Memory:
    return Memory(runtime)


class TestListOpRemoved:
    def test_list_symbol_no_longer_exported(self):
        assert not hasattr(memory_mod, "ListOp")
        assert not hasattr(memory_mod, "ListScope")

    def test_params_rejects_list_operation(self):
        """A raw ``op=list`` payload must fail validation (list is not a variant)."""
        with pytest.raises(ValidationError):
            Params.model_validate({"operation": {"op": "list", "scope": "persistent"}})

    def test_params_still_accepts_add(self):
        p = Params.model_validate(
            {"operation": {"op": "add", "kind": "user", "content": "x"}}
        )
        assert isinstance(p.operation, AddOp)
        # scope still defaults to persistent (unchanged by list removal).
        assert p.operation.scope == "persistent"

    def test_params_still_accepts_update_and_delete(self):
        pu = Params.model_validate(
            {"operation": {"op": "update", "id": "abc", "content": "y"}}
        )
        assert isinstance(pu.operation, UpdateOp)
        pd = Params.model_validate({"operation": {"op": "delete", "id": "abc"}})
        assert isinstance(pd.operation, DeleteOp)


class TestWriteOpsStillWork:
    async def test_add_session_scope_works(
        self, memory_tool: Memory, monkeypatch: pytest.MonkeyPatch
    ):
        async def _noop_approval(*_a, **_kw):
            return None

        monkeypatch.setattr(memory_tool, "_request_persistent_approval", _noop_approval)
        res = await memory_tool(
            Params(operation=AddOp(kind="user", scope="session", content="name is Jay"))
        )
        assert not res.is_error
        assert "id" in res.output

    async def test_update_and_delete_session_entry(self, memory_tool: Memory):
        from tests.conftest import tool_call_context

        # Add a session entry, then update + delete it by id (ids come from add /
        # the auto-injected memory in production — no list needed).
        add = await memory_tool(
            Params(operation=AddOp(kind="user", scope="session", content="v1"))
        )
        import json

        entry_id = json.loads(add.output)["id"]

        upd = await memory_tool(Params(operation=UpdateOp(id=entry_id, content="v2")))
        assert not upd.is_error

        with tool_call_context("Memory"):
            dele = await memory_tool(Params(operation=DeleteOp(id=entry_id)))
        assert not dele.is_error


class TestRecallInjectionUnaffected:
    """Recall (auto-injection of persistent memory at session start) reads storage
    DIRECTLY via ``alist_user_memory`` — it never used the Memory tool's ``list``
    op — so removing ``list`` from the tool does not affect recall.
    """

    async def test_read_persistent_uses_storage_alist(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        from pathlib import Path

        from kimi_cli.memory.entry import MemoryEntry
        from kimi_cli.soul.dynamic_injections import cross_session_memory as csm

        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "mysql")

        row = MemoryEntry(kind="user", scope="persistent", content="user's name is Jay")
        called: dict = {}

        class _FakeRemoteStorage:
            async def alist_user_memory(self, owner_id, limit=200):
                called["owner_id"] = owner_id
                return [row]

        # _read_persistent imports build_storage locally from kimi_cli.storage.
        monkeypatch.setattr(
            "kimi_cli.storage.build_storage", lambda: _FakeRemoteStorage()
        )

        # user_memory_dir layout: {share}/users/<owner_id>/memory
        user_memory_dir = Path(tmp_path) / "users" / "hechun-321" / "memory"
        user_memory_dir.mkdir(parents=True)

        entries = await csm._read_persistent(user_memory_dir)

        assert called["owner_id"] == "hechun-321"
        assert len(entries) == 1
        assert entries[0].content == "user's name is Jay"

    async def test_read_persistent_file_mode_reads_jsonl(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ):
        """file mode: recall still reads persistent.jsonl (unchanged, tool-independent)."""
        from pathlib import Path

        from kimi_cli.memory.entry import MemoryEntry
        from kimi_cli.memory.storage import append_entry
        from kimi_cli.soul.dynamic_injections import cross_session_memory as csm

        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "file")
        user_memory_dir = Path(tmp_path) / "users" / "hechun-9" / "memory"
        user_memory_dir.mkdir(parents=True)
        append_entry(
            user_memory_dir / "persistent.jsonl",
            MemoryEntry(kind="user", scope="persistent", content="from file"),
        )

        entries = await csm._read_persistent(user_memory_dir)
        assert [e.content for e in entries] == ["from file"]
