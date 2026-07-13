"""方案 B write-path e2e: Memory.add(persistent) → RemoteKimoStorage → wire → gateway.

# hechun-fork-cci

Production bug: the recall (list) path reached the gateway fine, but Memory tool
``add`` (append) never produced a memory-op on the gateway. This test drives the
REAL ``Memory._add(persistent)`` through a REAL ``Wire`` with a fake gateway that
drains the wire and resolves ``MemoryOpRequest`` — proving the append actually
round-trips (no MagicMock hiding the await / no deadlock / no swallow).
"""

from __future__ import annotations

import asyncio

import pytest

from kimi_cli.soul.agent import Runtime
from kimi_cli.storage.remote_storage import RemoteKimoStorage
from kimi_cli.tools.memory import AddOp, Memory, Params
from kimi_cli.wire import Wire
from kimi_cli.wire.types import MemoryOpRequest, MemoryOpResult


def _start_gateway(wire: Wire, seen: list[MemoryOpRequest]):
    """Subscribe the UI side, then drain + resolve MemoryOpRequests."""
    ui = wire.ui_side(merge=False)

    async def _drain():
        while True:
            try:
                msg = await ui.receive()
            except Exception:
                return
            if isinstance(msg, MemoryOpRequest):
                seen.append(msg)
                msg.resolve(MemoryOpResult(request_id=msg.id, ok=True))

    return asyncio.create_task(_drain())


@pytest.fixture
def memory_tool(runtime: Runtime) -> Memory:
    return Memory(runtime)


class TestMemoryAddReachesGateway:
    async def test_persistent_add_sends_append_over_wire(
        self, memory_tool: Memory, monkeypatch: pytest.MonkeyPatch
    ):
        # yolo: bypass approval (headless iOS/Flutter). Match production.
        async def _noop_approval(*_a, **_kw):
            return None

        monkeypatch.setattr(memory_tool, "_request_persistent_approval", _noop_approval)
        monkeypatch.setenv("KIMI_USER_ID", "hechun-777")
        monkeypatch.setenv("KIMI_STORAGE_BACKEND", "mysql")
        monkeypatch.setenv("KIMO_MEMORY_VIA_GATEWAY", "1")

        # Force the tool to use a RemoteKimoStorage (as it would on a CCI Pod).
        memory_tool._Memory__storage_singleton = RemoteKimoStorage(timeout_s=5.0)  # type: ignore[attr-defined]

        # Install a real wire + fake gateway BEFORE the add.
        from kimi_cli.soul import _current_wire

        wire = Wire()
        seen: list[MemoryOpRequest] = []
        gw = _start_gateway(wire, seen)
        token = _current_wire.set(wire)
        try:
            result = await memory_tool(
                Params(
                    operation=AddOp(
                        kind="user", scope="persistent", content="user's name is Jay"
                    )
                )
            )
        finally:
            _current_wire.reset(token)
            gw.cancel()
            wire.shutdown()

        assert not result.is_error
        # The gateway MUST have received exactly one append memory-op.
        assert len(seen) == 1, "append never reached the gateway over the wire"
        req = seen[0]
        assert req.op == "append"
        assert req.owner_id == "hechun-777"
        assert req.entry is not None
        assert req.entry["content"] == "user's name is Jay"

    async def test_persistent_add_no_wire_does_not_hang_or_crash(
        self, memory_tool: Memory, monkeypatch: pytest.MonkeyPatch
    ):
        """If no wire is present, add must return promptly (soft-fail), not hang."""

        async def _noop_approval(*_a, **_kw):
            return None

        monkeypatch.setattr(memory_tool, "_request_persistent_approval", _noop_approval)
        monkeypatch.setenv("KIMI_USER_ID", "hechun-1")
        memory_tool._Memory__storage_singleton = RemoteKimoStorage(timeout_s=0.3)  # type: ignore[attr-defined]

        from kimi_cli.soul import _current_wire

        token = _current_wire.set(None)
        try:
            result = await asyncio.wait_for(
                memory_tool(
                    Params(operation=AddOp(kind="user", scope="persistent", content="x"))
                ),
                timeout=3.0,
            )
        finally:
            _current_wire.reset(token)
        assert not result.is_error


class TestRootCauseNotAPlumbingBug:
    """Document the two upstream reasons an ``add`` produces no gateway memory-op —
    NEITHER is a wire/storage plumbing defect. These reproduce the production
    symptom (thinking shows Memory.add, gateway sees no append) deterministically.
    """

    async def test_session_scope_never_reaches_gateway(
        self, memory_tool: Memory, monkeypatch: pytest.MonkeyPatch
    ):
        """scope=session lands in SessionState only — no MemoryOpRequest is sent."""
        from kimi_cli.soul import _current_wire

        wire = Wire()
        seen: list[MemoryOpRequest] = []
        gw = _start_gateway(wire, seen)
        token = _current_wire.set(wire)
        # Use RemoteKimoStorage so *if* it wrongly routed, we'd catch an append.
        memory_tool._Memory__storage_singleton = RemoteKimoStorage(timeout_s=1.0)  # type: ignore[attr-defined]
        try:
            result = await memory_tool(
                Params(operation=AddOp(kind="user", scope="session", content="name is Jay"))
            )
        finally:
            _current_wire.reset(token)
            gw.cancel()
            wire.shutdown()
        assert not result.is_error
        assert seen == [], "session-scope add must NOT emit a gateway memory-op"

    async def test_approval_reject_never_reaches_storage(
        self, memory_tool: Memory, monkeypatch: pytest.MonkeyPatch
    ):
        """If the approval gate rejects (headless client, no yolo), the storage /
        wire path is never entered → no gateway append."""
        from kosong.tooling import ToolError

        async def _reject(*_a, **_kw):
            return ToolError(message="rejected", brief="rejected")

        monkeypatch.setattr(memory_tool, "_request_persistent_approval", _reject)

        from kimi_cli.soul import _current_wire

        wire = Wire()
        seen: list[MemoryOpRequest] = []
        gw = _start_gateway(wire, seen)
        token = _current_wire.set(wire)
        memory_tool._Memory__storage_singleton = RemoteKimoStorage(timeout_s=1.0)  # type: ignore[attr-defined]
        try:
            result = await memory_tool(
                Params(operation=AddOp(kind="user", scope="persistent", content="x"))
            )
        finally:
            _current_wire.reset(token)
            gw.cancel()
            wire.shutdown()
        assert result.is_error  # the rejection surfaces
        assert seen == [], "rejected add must NOT emit a gateway memory-op"
