"""RemoteKimoStorage: worker→gateway wire delegation of persistent memory.

# hechun-fork-cci (方案 B)

The CCI worker cannot reach RDS, so :class:`RemoteKimoStorage` sends a
``MemoryOpRequest`` over the wire and the gateway answers with a
``MemoryOpResult``. These tests stand up a real :class:`Wire`, install it via the
soul context var, and drive a *fake gateway* that drains the UI side and resolves
requests — verifying the request shape and result plumbing without a real Pod or
RDS. The gateway-side executor (``_run_gateway_memory_op``) is tested separately
against a fake ``KimoStorage``.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from kimi_cli.memory.entry import MemoryEntry
from kimi_cli.storage.remote_storage import RemoteKimoStorage
from kimi_cli.wire import Wire
from kimi_cli.wire.types import MemoryOpRequest, MemoryOpResult


async def _fake_gateway(ui, handler) -> None:
    """Drain the wire UI side and let ``handler`` resolve MemoryOpRequests.

    ``ui`` must be subscribed *before* the storage sends, otherwise the
    BroadcastQueue drops the message (subscribe-then-receive semantics).
    """
    while True:
        try:
            msg = await ui.receive()
        except Exception:
            return
        if isinstance(msg, MemoryOpRequest):
            handler(msg)


def _start_gateway(wire: Wire, handler):
    """Subscribe the UI side synchronously, then spawn the drain task."""
    ui = wire.ui_side(merge=False)
    return asyncio.create_task(_fake_gateway(ui, handler))


@pytest.fixture
def wire_ctx():
    """Install a real Wire into the soul context var; tear it down after."""
    from kimi_cli.soul import _current_wire

    wire = Wire()
    token = _current_wire.set(wire)
    try:
        yield wire
    finally:
        _current_wire.reset(token)
        wire.shutdown()


class TestRemoteAppend:
    async def test_append_sends_request_and_resolves(self, wire_ctx: Wire):
        seen: list[MemoryOpRequest] = []

        def handler(req: MemoryOpRequest) -> None:
            seen.append(req)
            req.resolve(MemoryOpResult(request_id=req.id, ok=True))

        gw = _start_gateway(wire_ctx, handler)

        storage = RemoteKimoStorage(timeout_s=5.0)
        entry = MemoryEntry(kind="user", scope="persistent", content="prefers metric")
        sid = uuid4()
        object.__setattr__(entry, "source_kimo_session_id", sid)

        await storage.aappend_user_memory("hechun-42", entry)

        gw.cancel()
        assert len(seen) == 1
        req = seen[0]
        assert req.op == "append"
        assert req.owner_id == "hechun-42"
        assert req.entry is not None
        assert req.entry["content"] == "prefers metric"
        # source_kimo_session_id folded into the payload for the gateway.
        assert req.entry["source_kimo_session_id"] == str(sid)

    async def test_append_no_wire_is_soft(self, monkeypatch: pytest.MonkeyPatch):
        """No current wire → swallow (no raise), matching §5.5."""
        from kimi_cli.soul import _current_wire

        token = _current_wire.set(None)
        try:
            storage = RemoteKimoStorage(timeout_s=0.2)
            entry = MemoryEntry(kind="user", scope="persistent", content="x")
            # Must not raise even though there's no wire to send on.
            await storage.aappend_user_memory("hechun-1", entry)
        finally:
            _current_wire.reset(token)

    async def test_append_timeout_is_soft(self, wire_ctx: Wire):
        """Gateway never answers → timeout is swallowed."""

        def handler(_req: MemoryOpRequest) -> None:
            pass  # never resolve

        gw = _start_gateway(wire_ctx, handler)
        storage = RemoteKimoStorage(timeout_s=0.2)
        entry = MemoryEntry(kind="user", scope="persistent", content="x")
        await storage.aappend_user_memory("hechun-1", entry)  # returns, no raise
        gw.cancel()


class TestRemoteList:
    async def test_list_round_trips_entries(self, wire_ctx: Wire):
        row = MemoryEntry(kind="reference", scope="persistent", content="uses pump")

        def handler(req: MemoryOpRequest) -> None:
            assert req.op == "list"
            req.resolve(
                MemoryOpResult(
                    request_id=req.id,
                    ok=True,
                    entries=[row.model_dump(mode="json")],
                )
            )

        gw = _start_gateway(wire_ctx, handler)
        storage = RemoteKimoStorage(timeout_s=5.0)
        out = await storage.alist_user_memory("hechun-7")
        gw.cancel()

        assert len(out) == 1
        assert out[0].content == "uses pump"
        assert out[0].kind == "reference"

    async def test_list_not_ok_returns_empty(self, wire_ctx: Wire):
        def handler(req: MemoryOpRequest) -> None:
            req.resolve(MemoryOpResult(request_id=req.id, ok=False, error="db down"))

        gw = _start_gateway(wire_ctx, handler)
        storage = RemoteKimoStorage(timeout_s=5.0)
        out = await storage.alist_user_memory("hechun-7")
        gw.cancel()
        assert out == []


class TestGatewayExecutor:
    """The gateway-side executor runs the op against its local KimoStorage."""

    def test_append_calls_storage_and_stamps_source(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from kimi_cli.web.runner import process as proc

        captured: dict = {}

        class _FakeStorage:
            def append_user_memory(self, owner_id, entry):
                captured["owner_id"] = owner_id
                captured["entry"] = entry

            def list_user_memory(self, owner_id, limit=200):
                return []

        monkeypatch.setattr(proc, "_gateway_memory_storage", _FakeStorage())

        sid = uuid4()
        entry = MemoryEntry(kind="user", scope="persistent", content="hi")
        payload = entry.model_dump(mode="json")
        payload["source_kimo_session_id"] = str(sid)
        req = MemoryOpRequest(id="r1", op="append", owner_id="hechun-9", entry=payload)

        result = proc._run_gateway_memory_op(req)
        assert result.ok is True
        assert captured["owner_id"] == "hechun-9"
        stamped = getattr(captured["entry"], "source_kimo_session_id", None)
        assert isinstance(stamped, UUID)
        assert stamped == sid

    def test_list_serializes_entries(self, monkeypatch: pytest.MonkeyPatch):
        from kimi_cli.web.runner import process as proc

        rows = [MemoryEntry(kind="project", scope="persistent", content="c1")]

        class _FakeStorage:
            def append_user_memory(self, owner_id, entry):
                pass

            def list_user_memory(self, owner_id, limit=200):
                return rows

        monkeypatch.setattr(proc, "_gateway_memory_storage", _FakeStorage())
        req = MemoryOpRequest(id="r2", op="list", owner_id="hechun-9")
        result = proc._run_gateway_memory_op(req)
        assert result.ok is True
        assert len(result.entries) == 1
        assert result.entries[0]["content"] == "c1"

    def test_storage_error_returns_not_ok(self, monkeypatch: pytest.MonkeyPatch):
        from kimi_cli.web.runner import process as proc

        class _BoomStorage:
            def append_user_memory(self, owner_id, entry):
                raise RuntimeError("rds unreachable")

            def list_user_memory(self, owner_id, limit=200):
                raise RuntimeError("rds unreachable")

        monkeypatch.setattr(proc, "_gateway_memory_storage", _BoomStorage())
        entry = MemoryEntry(kind="user", scope="persistent", content="x")
        req = MemoryOpRequest(
            id="r3", op="append", owner_id="hechun-9", entry=entry.model_dump(mode="json")
        )
        result = proc._run_gateway_memory_op(req)
        assert result.ok is False
        assert "rds unreachable" in result.error
