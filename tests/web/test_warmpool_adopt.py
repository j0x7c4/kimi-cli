"""Adoption path: CCISessionProcess taking over a warm Pod (W2↔W3 seam).

# hechun-fork-cci (warm pool)

Covers the three things that are only visible where the two sides meet:
frame ordering (bind before initialize), reclaim semantics (a warm Pod is
invisible to the idle sweeper; a claimed one is not), and the worker→gateway
diagnostic channel.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from kimi_cli.web.app import _classify_reclaim, _reclaim_idle_cci_sessions
from kimi_cli.web.runner import warm_protocol as wp
from kimi_cli.web.runner.cci_process import CCIRunner
from kimi_cli.web.spawner import SandboxHandle
from tests.web.test_cci_session_process import FakeExecStream


class FakeWarmPool:
    """A WarmPoolManager stand-in that always hits (or always misses)."""

    def __init__(self, stream=None, *, hit: bool = True):
        self.stream = stream
        self.hit = hit
        self.acquired: list = []
        self.released: list = []

    async def acquire(self, session_id, owner_id, *, yolo, agent, env):
        self.acquired.append({"sid": session_id, "owner": owner_id, "yolo": yolo, "agent": agent})
        if not self.hit:
            return None
        handle = SandboxHandle(backend="cci", handle_id="kimo-sandbox-warm-1")
        # The real manager writes the bind frame + waits for the confirmation
        # before returning; mirror that so ordering assertions are meaningful.
        await self.stream.sendall(
            wp.encode(
                wp.FRAME_BIND, session_id=str(session_id), owner_id=owner_id, yolo=yolo
            ).encode("utf-8")
        )
        return handle, self.stream

    async def release(self, pod_name, reason):
        self.released.append((pod_name, reason))


class FakeSpawner:
    def __init__(self, stream):
        self._stream = stream
        self.spawned: list = []
        self.stopped: list = []

    async def spawn(self, sid, owner_id, env, *, warm=False, pod_name=None):
        self.spawned.append(sid)
        return SandboxHandle(backend="cci", handle_id=f"kimo-sandbox-{sid}")

    async def attach(self, handle, *, command=None):
        return self._stream

    async def stop(self, handle):
        self.stopped.append(handle.handle_id)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    """Keep these tests off the disk, the env and any real database."""
    import kimi_cli.web.runner.cci_process as cp

    # start() asserts isinstance(stream, KimoExecStream); the fake stands in for it
    # (same trick as tests/web/test_cci_session_process.py).
    monkeypatch.setattr(cp, "KimoExecStream", FakeExecStream)
    monkeypatch.setattr(cp, "_read_owner_id_from_disk", lambda sid: "hechun-5")
    monkeypatch.setattr(cp, "_read_agent_name_from_disk", lambda sid: "diabetes-expert")
    monkeypatch.setattr(cp, "get_clean_env", lambda: {})
    monkeypatch.setattr(cp, "_resolve_bind_identity", lambda sid, env: ("hechun-5", True))


async def _make_proc(stream, pool):
    runner = CCIRunner(spawner=FakeSpawner(stream), warm_pool=pool)
    return runner, await runner.get_or_create_session(uuid4())


class TestAdoptionOrdering:
    async def test_bind_is_written_before_initialize_is_replayed(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """🔴 initialize must NOT reach the worker before the bind completes.

        Driven through the REAL WarmPoolManager (not the stub) because that is
        where the ordering is actually enforced: ``acquire`` writes the bind and
        waits for the worker's confirmation before it hands the stream back, and
        only then does ``start`` replay initialize. A warm worker is blocked
        reading its bind line — an initialize frame arriving first would be
        consumed *as* that line, so the bind would fail and the capability
        handshake would vanish (the AskUserQuestion failure mode, new disguise).
        """
        from kimi_cli.web.warmpool.manager import WarmPoolManager

        from tests.web.test_warmpool import FakeSpawner as WarmSpawner
        from tests.web.test_warmpool import FakeStore, FakeWarmStream

        import kimi_cli.web.runner.cci_process as cp

        stream = FakeWarmStream()
        monkeypatch.setattr(cp, "KimoExecStream", FakeWarmStream)
        warm_spawner = WarmSpawner([stream])
        mgr = WarmPoolManager(
            spawner=warm_spawner,
            store=FakeStore(),
            env_builder=dict,
            agent="diabetes-expert",
            ready_timeout_s=2.0,
            bind_timeout_s=1.0,
        )
        assert await mgr.refill() == 1

        runner = CCIRunner(spawner=FakeSpawner(stream), warm_pool=mgr)
        proc = await runner.get_or_create_session(uuid4())
        proc._last_initialize_frame = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        )

        await proc.start()
        try:
            written = [w.decode("utf-8") for w in stream.written]
            bind_at = next(
                i
                for i, w in enumerate(written)
                if (wp.decode(w) or {}).get(wp.WARM_KEY) == wp.FRAME_BIND
            )
            init_at = next(i for i, w in enumerate(written) if "initialize" in w)
            assert bind_at < init_at, written
        finally:
            await proc.stop_worker(emit_status=False)

    async def test_adoption_skips_the_spawn(self):
        stream = FakeExecStream()
        pool = FakeWarmPool(stream)
        runner, proc = await _make_proc(stream, pool)
        spawner = proc._spawner

        await proc.start()
        try:
            assert spawner.spawned == []  # no Pod was created
            assert proc.handle.handle_id == "kimo-sandbox-warm-1"
            assert pool.acquired[0]["owner"] == "hechun-5"
            assert pool.acquired[0]["yolo"] is True
        finally:
            await proc.stop_worker(emit_status=False)

    async def test_pool_miss_falls_back_to_a_normal_spawn(self):
        stream = FakeExecStream()
        pool = FakeWarmPool(stream, hit=False)
        runner, proc = await _make_proc(stream, pool)

        await proc.start()
        try:
            assert proc._spawner.spawned == [proc.session_id]
            assert proc.handle.handle_id == f"kimo-sandbox-{proc.session_id}"
        finally:
            await proc.stop_worker(emit_status=False)

    async def test_a_broken_pool_never_fails_the_session(self):
        class ExplodingPool(FakeWarmPool):
            async def acquire(self, *a, **kw):
                raise RuntimeError("pool is down")

        stream = FakeExecStream()
        runner, proc = await _make_proc(stream, ExplodingPool(stream))

        await proc.start()  # must not raise
        try:
            assert proc._spawner.spawned == [proc.session_id]
        finally:
            await proc.stop_worker(emit_status=False)

    async def test_claimed_row_is_closed_when_the_session_stops(self):
        stream = FakeExecStream()
        pool = FakeWarmPool(stream)
        runner, proc = await _make_proc(stream, pool)
        await proc.start()
        await proc.stop_worker(reason="idle_reclaim", emit_status=False)
        assert pool.released == [("kimo-sandbox-warm-1", "idle_reclaim")]


class TestReclaimVisibility:
    async def test_warm_pods_are_invisible_to_the_idle_sweeper(self):
        """Warm Pods are not registered in the runner — that IS the exemption.

        Registering them under a placeholder session id would force an explicit
        carve-out in the sweeper (and every other ``iter_sessions`` consumer) to
        undo a registration that buys nothing. Keeping them out of the runner
        makes the exemption structural and impossible to forget.
        """
        stream = FakeExecStream()
        pool = FakeWarmPool(stream)
        runner = CCIRunner(spawner=FakeSpawner(stream), warm_pool=pool)

        assert list(runner.iter_sessions()) == []
        assert await _reclaim_idle_cci_sessions(runner, idle_ttl_s=0) == 0

    async def test_a_claimed_session_is_reclaimable_immediately(self):
        """🔴 The instant a Pod is claimed, ordinary idle reclaim applies.

        This is the check that keeps the pool from becoming a new entry point
        for the old "busy session never self-heals" class of bug: an adopted Pod
        is a plain runner session, with no lingering warm-pool privilege.
        """
        stream = FakeExecStream()
        pool = FakeWarmPool(stream)
        runner, proc = await _make_proc(stream, pool)
        await proc.start()
        try:
            assert [sid for sid, _ in runner.iter_sessions()] == [proc.session_id]
            assert (
                _classify_reclaim(
                    state="idle",
                    is_alive=True,
                    last_active_at=0.0,
                    now=10_000.0,
                    idle_ttl_s=900,
                    dead_ttl_s=30,
                )
                == "idle_reclaim"
            )
        finally:
            await proc.stop_worker(emit_status=False)


class TestWorkerDiagFrame:
    async def test_diag_lines_are_consumed_not_broadcast(self):
        """Timing frames go to the gateway log only.

        The M0 diagnostic build wrote a bare text line onto the JSON-RPC stream:
        the gateway could only report it as ``Invalid JSONRPC out message``, and
        it was forwarded verbatim to every connected WebSocket client first.
        """
        diag = json.dumps({"kimo_diag": "worker_timing", "sid": "x", "line": "…"})
        rpc = json.dumps({"jsonrpc": "2.0", "id": "a", "result": {"ok": 1}})
        stream = FakeExecStream([diag.encode() + b"\n", rpc.encode() + b"\n"])
        runner, proc = await _make_proc(stream, FakeWarmPool(stream, hit=False))

        broadcast: list[str] = []

        async def _record(message: str) -> None:
            broadcast.append(message)

        proc._broadcast = _record  # type: ignore[method-assign]
        await proc.start()
        assert proc._read_task is not None
        await proc._read_task  # runs to EOF

        assert all("kimo_diag" not in m for m in broadcast), broadcast
        assert rpc in broadcast, broadcast


class TestWarmPodEnv:
    def test_warm_pod_env_carries_no_identity(self, monkeypatch: pytest.MonkeyPatch):
        """🔴 A warm Pod must be created without a user in its env.

        MCP headers embed ``${KIMI_USER_ID}`` and are substituted from
        ``os.environ`` when the client is built; a baked-in placeholder (or a
        leftover real user) would ride along into connections that are never
        rebuilt after the bind — tools failing outright is the *good* outcome,
        the bad one is one user's tools carrying another user's identity
        (spec §4.4).
        """
        import kimi_cli.web.runner.cci_process as cp

        monkeypatch.setattr(
            cp,
            "get_clean_env",
            lambda: {"KIMI_API_KEY": "k", "KIMO_GATEWAY_INTERNAL_URL": "http://gw:5494"},
        )
        env = cp.build_warm_sandbox_env("diabetes-expert")

        assert "KIMI_USER_ID" not in env
        assert "KIMI_SESSION_ID" not in env
        # …but everything session-independent IS there, so the Pod can prepare.
        assert env["SUBAGENT"] == "diabetes-expert"
        assert env["KIMI_REQUIRE_AGENT"] == "1"
        assert env["KIMO_SANDBOX_ASSETS_URL"].startswith("http://gw:5494")
