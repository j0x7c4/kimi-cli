"""Warm-pool gateway tests (WarmPoolManager + adoption path).

# hechun-fork-cci (warm pool)

Scope note, stated plainly: the atomic claim is a MySQL ``UPDATE ... ORDER BY
... LIMIT 1``. SQLite cannot execute that form, so the *mutual exclusion of the
statement itself* is not unit-testable here — :class:`FakeStore` models a DB
that provides it. What IS tested here is everything the gateway builds on top:
that exactly one concurrent acquirer walks away with the Pod, that every failure
degrades to a cold start, and that the frozen SQL text has not drifted (a
separate test asserts the literal clauses, so nobody "optimises" the claim into
a non-atomic SELECT-then-UPDATE).
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest

from kimi_cli.web.runner import warm_protocol as wp
from kimi_cli.web.spawner import SandboxHandle
from kimi_cli.web.warmpool.manager import WarmPoolManager
from kimi_cli.web.warmpool.store import (
    STATE_CLAIMED,
    STATE_DEAD,
    STATE_READY,
    STATE_WARMING,
)


class FakeWarmStream:
    """Models a warm worker over the exec stream.

    ``sendall`` interprets warm frames exactly like the real worker does
    (ping → pong, bind → bound) so the manager talks to something that answers.
    """

    def __init__(self, *, ready_ok: bool = True, ready_reason=None, bind_reply: str = "bound"):
        self.out: asyncio.Queue[bytes] = asyncio.Queue()
        self.written: list[bytes] = []
        self.closed = False
        self._bind_reply = bind_reply
        self.out.put_nowait(
            wp.encode(
                wp.FRAME_READY,
                ok=ready_ok,
                reason=ready_reason,
                agent="diabetes-expert",
                agent_path="/root/.kimi/agents/diabetes-expert.yaml",
                pid=1,
                prepare_ms=100,
            ).encode("utf-8")
        )

    async def readline(self) -> bytes:
        return await self.out.get()

    async def sendall(self, data: bytes) -> None:
        self.written.append(data)
        frame = wp.decode(data)
        if frame is None:
            return
        kind = frame.get(wp.WARM_KEY)
        if kind == wp.FRAME_PING:
            await self.out.put(
                wp.encode(wp.FRAME_PONG, seq=frame.get("seq"), uptime_ms=1).encode("utf-8")
            )
        elif kind == wp.FRAME_BIND:
            if self._bind_reply == "bound":
                await self.out.put(
                    wp.encode(wp.FRAME_BOUND, session_id=frame["session_id"]).encode("utf-8")
                )
            elif self._bind_reply == "error":
                await self.out.put(
                    wp.encode(
                        wp.FRAME_ERROR, reason=wp.REASON_BIND_INVALID, detail="owner_id missing"
                    ).encode("utf-8")
                )
            # "silent" → no reply at all (models a wedged worker)

    async def close(self) -> None:
        self.closed = True

    def at_eof(self) -> bool:
        return self.closed


class DeadStream(FakeWarmStream):
    """A Pod whose worker never says anything (EOF on every read)."""

    async def readline(self) -> bytes:
        return b""


class FakeSpawner:
    def __init__(self, streams=None):
        self.spawned: list[dict] = []
        self.stopped: list[str] = []
        self._streams = list(streams or [])
        self.attach_commands: list[list[str] | None] = []

    async def spawn(self, sid, owner_id, env, *, warm=False, pod_name=None):
        self.spawned.append(
            {"sid": sid, "owner": owner_id, "env": env, "warm": warm, "pod": pod_name}
        )
        return SandboxHandle(
            backend="cci",
            handle_id=pod_name or f"kimo-sandbox-{sid}",
            network_endpoint="ws://10.0.0.7:5494",
            meta={"pod_ip": "10.0.0.7"},
        )

    async def attach(self, handle, *, command=None):
        self.attach_commands.append(command)
        return self._streams.pop(0) if self._streams else FakeWarmStream()

    async def stop(self, handle):
        self.stopped.append(handle.handle_id)


class FakeStore:
    """In-memory stand-in for the ``kimo_sandbox_pod`` table.

    ``claim`` is atomic under a threading lock, mirroring what the single
    conditional UPDATE guarantees in MySQL.
    """

    def __init__(self):
        import threading

        self.rows: dict[str, dict] = {}
        self._lock = threading.Lock()

    def insert_warming(self, pod_name, placeholder):
        self.rows[pod_name] = {
            "pod_name": pod_name,
            "state": STATE_WARMING,
            "kimo_session_id": placeholder,
            "owner_id": None,
            "pod_ip": None,
            "endpoint": None,
            "dead_reason": None,
            "seq": len(self.rows),
        }

    def mark_ready(self, pod_name, *, pod_ip, endpoint):
        row = self.rows.get(pod_name)
        if row is None or row["state"] != STATE_WARMING:
            return False
        row.update(state=STATE_READY, pod_ip=pod_ip, endpoint=endpoint)
        return True

    def claim(self, session_id, owner_id):
        with self._lock:
            candidates = [
                r
                for r in sorted(self.rows.values(), key=lambda r: r["seq"])
                if r["state"] == STATE_READY and r["owner_id"] is None
            ]
            if not candidates:
                return None
            row = candidates[0]
            row.update(state=STATE_CLAIMED, kimo_session_id=session_id, owner_id=owner_id)
            return dict(row)

    def mark_dead(self, pod_name, reason, *, expect_states=None):
        row = self.rows.get(pod_name)
        if row is None or (expect_states and row["state"] not in expect_states):
            return False
        row.update(state=STATE_DEAD, dead_reason=reason)
        return True

    def mark_all_live_dead(self, reason):
        names = [n for n, r in self.rows.items() if r["state"] in (STATE_WARMING, STATE_READY)]
        for n in names:
            self.rows[n].update(state=STATE_DEAD, dead_reason=reason)
        return names

    def touch_health(self, pod_name):
        self.rows.get(pod_name, {})["last_health_at"] = "now"

    def counts(self):
        out: dict[str, int] = {}
        for r in self.rows.values():
            out[r["state"]] = out.get(r["state"], 0) + 1
        return out

    def list_by_states(self, states):
        return [dict(r) for r in self.rows.values() if r["state"] in states]


def _manager(spawner, store, **kw) -> WarmPoolManager:
    kw.setdefault("size", 1)
    return WarmPoolManager(
        spawner=spawner,
        store=store,
        env_builder=lambda: {"SUBAGENT": "diabetes-expert", "KIMI_REQUIRE_AGENT": "1"},
        agent="diabetes-expert",
        ready_timeout_s=2.0,
        bind_timeout_s=1.0,
        ping_timeout_s=1.0,
        **kw,
    )


class TestRefill:
    async def test_warms_a_pod_and_marks_it_ready(self):
        spawner = FakeSpawner()
        store = FakeStore()
        mgr = _manager(spawner, store)

        assert await mgr.refill() == 1

        assert spawner.spawned[0]["warm"] is True
        # Warm Pods are named independently of any session id (spec §5).
        pod_name = spawner.spawned[0]["pod"]
        assert pod_name.startswith("kimo-sandbox-")
        assert store.rows[pod_name]["state"] == STATE_READY
        # The identity-free env is what the Pod is created with.
        assert "KIMI_USER_ID" not in spawner.spawned[0]["env"]
        assert "KIMI_SESSION_ID" not in spawner.spawned[0]["env"]
        # Two-phase worker entry, not /start-sandbox.sh.
        assert spawner.attach_commands[0] == [
            "python",
            "-m",
            "kimi_cli.web.runner.worker",
            "--warm",
        ]

    async def test_pod_reporting_assets_missing_is_never_pooled(self):
        """``warming → ready`` must verify the assets actually landed.

        ``_fetch_sandbox_assets`` logs download failures without raising, so a
        Pod can boot fine and still be unable to serve a single claim. Such a Pod
        must be destroyed, not pooled.
        """
        stream = FakeWarmStream(ready_ok=False, ready_reason=wp.REASON_ASSETS_MISSING)
        spawner = FakeSpawner([stream])
        store = FakeStore()
        mgr = _manager(spawner, store)

        assert await mgr.refill() == 0

        pod_name = spawner.spawned[0]["pod"]
        assert store.rows[pod_name]["state"] == STATE_DEAD
        assert store.rows[pod_name]["dead_reason"] == wp.REASON_ASSETS_MISSING
        assert spawner.stopped == [pod_name]  # Pod deleted, not left running
        assert mgr.stats()["pooled"] == 0

    async def test_repeated_failures_back_off(self):
        spawner = FakeSpawner([DeadStream(), DeadStream()])
        mgr = _manager(spawner, FakeStore())
        await mgr.refill()
        first = mgr.stats()["backoff_s"]
        await mgr.refill()
        assert first > 0
        assert mgr.stats()["backoff_s"] > first


class TestAcquire:
    async def test_claim_binds_then_returns_the_pod(self):
        spawner = FakeSpawner()
        store = FakeStore()
        mgr = _manager(spawner, store)
        await mgr.refill()
        pod_name = spawner.spawned[0]["pod"]
        stream = mgr._pods[pod_name].stream

        sid = uuid4()
        claimed = await mgr.acquire(
            sid, "hechun-42", yolo=True, agent="diabetes-expert", env={"SUBAGENT": "x"}
        )

        assert claimed is not None
        handle, got_stream = claimed
        assert handle.handle_id == pod_name
        assert got_stream is stream
        assert store.rows[pod_name]["state"] == STATE_CLAIMED
        assert store.rows[pod_name]["owner_id"] == "hechun-42"
        assert store.rows[pod_name]["kimo_session_id"] == str(sid)
        # The bind frame carries the identity — the worker cannot read it itself.
        bind = wp.decode(stream.written[0])
        assert bind[wp.WARM_KEY] == wp.FRAME_BIND
        assert bind["session_id"] == str(sid)
        assert bind["owner_id"] == "hechun-42"
        assert bind["yolo"] is True

    async def test_exactly_one_of_many_concurrent_claims_wins(self):
        spawner = FakeSpawner()
        store = FakeStore()
        mgr = _manager(spawner, store)
        await mgr.refill()

        results = await asyncio.gather(
            *(
                mgr.acquire(uuid4(), f"user-{i}", yolo=True, agent="diabetes-expert", env={})
                for i in range(8)
            )
        )
        winners = [r for r in results if r is not None]
        assert len(winners) == 1
        # Everyone else degrades to a cold start — no exceptions, no partial state.
        assert mgr.stats()["hits"] == 1
        assert mgr.stats()["misses"] == 7

    async def test_empty_pool_returns_none(self):
        mgr = _manager(FakeSpawner(), FakeStore())
        assert await mgr.acquire(uuid4(), "u", yolo=True, agent="diabetes-expert", env={}) is None

    async def test_missing_owner_never_burns_a_pod(self):
        """An empty owner cannot produce a valid bind, so don't spend the Pod."""
        spawner = FakeSpawner()
        store = FakeStore()
        mgr = _manager(spawner, store)
        await mgr.refill()
        pod_name = spawner.spawned[0]["pod"]

        assert await mgr.acquire(uuid4(), "", yolo=True, agent="diabetes-expert", env={}) is None
        assert store.rows[pod_name]["state"] == STATE_READY  # still pooled

    async def test_agent_mismatch_falls_back_to_cold_start(self):
        spawner = FakeSpawner()
        store = FakeStore()
        mgr = _manager(spawner, store)
        await mgr.refill()
        pod_name = spawner.spawned[0]["pod"]

        assert await mgr.acquire(uuid4(), "u", yolo=True, agent="pump-coach", env={}) is None
        assert store.rows[pod_name]["state"] == STATE_READY

    async def test_bind_rejected_by_worker_degrades_and_evicts(self):
        """Worker rejects the bind → cold start, and the Pod is destroyed."""
        spawner = FakeSpawner([FakeWarmStream(bind_reply="error")])
        store = FakeStore()
        mgr = _manager(spawner, store)
        await mgr.refill()
        pod_name = spawner.spawned[0]["pod"]

        assert await mgr.acquire(uuid4(), "u", yolo=True, agent="diabetes-expert", env={}) is None
        assert store.rows[pod_name]["state"] == STATE_DEAD
        assert store.rows[pod_name]["dead_reason"] == "bind_failed"
        assert spawner.stopped == [pod_name]
        assert mgr.stats()["pooled"] == 0

    async def test_wedged_worker_times_out_and_degrades(self):
        spawner = FakeSpawner([FakeWarmStream(bind_reply="silent")])
        store = FakeStore()
        mgr = _manager(spawner, store)
        await mgr.refill()
        pod_name = spawner.spawned[0]["pod"]

        assert await mgr.acquire(uuid4(), "u", yolo=True, agent="diabetes-expert", env={}) is None
        assert store.rows[pod_name]["state"] == STATE_DEAD


class TestRestartReconcile:
    async def test_startup_marks_live_rows_dead_so_no_claim_hits_a_deleted_pod(self):
        """Gateway restart deletes every Pod; the rows must not survive it.

        A surviving ``ready`` row is worse than an empty pool: the claim
        succeeds, the session is wired to a Pod that no longer exists, and the
        cold-start fallback never runs (spec §9).
        """
        store = FakeStore()
        store.insert_warming("kimo-sandbox-old-1", "00000000-0000-4000-8000-aaaaaaaaaaaa")
        store.mark_ready("kimo-sandbox-old-1", pod_ip="10.0.0.1", endpoint="ws://10.0.0.1:5494")
        store.insert_warming("kimo-sandbox-old-2", "00000000-0000-4000-8000-bbbbbbbbbbbb")

        mgr = _manager(FakeSpawner(), store, refill_interval_s=3600)
        await mgr.start()
        try:
            assert store.rows["kimo-sandbox-old-1"]["state"] == STATE_DEAD
            assert store.rows["kimo-sandbox-old-1"]["dead_reason"] == "gateway_restart"
            assert store.rows["kimo-sandbox-old-2"]["state"] == STATE_DEAD
            # And a claim right after restart finds nothing → cold start.
            assert (
                await mgr.acquire(uuid4(), "u", yolo=True, agent="diabetes-expert", env={}) is None
            )
        finally:
            await mgr.close()


class TestProbe:
    async def test_live_pod_pongs_and_stays(self):
        spawner = FakeSpawner()
        store = FakeStore()
        mgr = _manager(spawner, store)
        await mgr.refill()
        pod_name = spawner.spawned[0]["pod"]

        result = await mgr.probe()

        assert result["probed"] == [{"pod_name": pod_name, "alive": True, "detail": ""}]
        assert store.rows[pod_name]["state"] == STATE_READY

    async def test_wedged_worker_is_evicted_and_replaced(self):
        """Pod ``Running`` but worker not answering → the only eviction signal.

        Without this the bad Pod would sit in the pool forever (no age-based
        eviction by design) and fail every claim.
        """

        class MuteStream(FakeWarmStream):
            async def sendall(self, data: bytes) -> None:
                self.written.append(data)  # swallow: never answers a ping

        spawner = FakeSpawner([MuteStream(), FakeWarmStream()])
        store = FakeStore()
        mgr = _manager(spawner, store)
        await mgr.refill()
        bad = spawner.spawned[0]["pod"]

        result = await mgr.probe()

        assert result["probed"][0]["alive"] is False
        assert store.rows[bad]["state"] == STATE_DEAD
        assert store.rows[bad]["dead_reason"] == "probe_failed"
        assert bad in spawner.stopped
        # …and the pool was refilled with a fresh Pod.
        assert result["refilled"] == 1
        assert mgr.stats()["pooled"] == 1


class TestFrozenClaimSQL:
    def test_claim_statement_keeps_the_frozen_shape(self):
        """The claim must stay ONE conditional UPDATE (plan §0.3).

        Its atomicity is the entire mutual-exclusion mechanism: split it into
        SELECT-then-UPDATE and two gateways can hand the same Pod to two users.
        """
        import inspect

        from kimi_cli.web.warmpool.store import WarmPoolStore

        src = inspect.getsource(WarmPoolStore._claim_once)
        assert "UPDATE" in src
        assert "SET state='claimed'" in src
        assert "WHERE state='ready' AND owner_id IS NULL" in src
        assert "ORDER BY created_at LIMIT 1" in src
        # The winning row is re-read only AFTER the UPDATE decided the winner.
        update_at = src.index("UPDATE")
        select_at = src.index("SELECT")
        assert update_at < select_at

    def test_deadlock_is_normalised_into_a_pool_miss(self):
        """MySQL 1213 must degrade like an empty pool, never surface as an error.

        W1 measured 39 deadlocks in 60 concurrent claims: the UPDATE takes the
        secondary index and the PK in an order two connections can disagree on.
        InnoDB still commits exactly one winner — but if the loser's exception
        escapes, a case that is supposed to be a silent cold start becomes a
        user-visible AI-assistant error (plan §0.3).
        """
        from kimi_cli.web.warmpool.store import WarmPoolStore

        store = WarmPoolStore.__new__(WarmPoolStore)

        class _Deadlock(Exception):
            pass

        def _boom(*_a, **_k):
            raise _Deadlock(1213, "Deadlock found when trying to get lock")

        store._claim_once = _boom  # type: ignore[method-assign]
        assert store.claim("sid", "owner") is None

    def test_non_deadlock_db_errors_still_propagate(self):
        """Only 1213 is swallowed — an outage must not masquerade as a miss."""
        from kimi_cli.web.warmpool.store import WarmPoolStore

        store = WarmPoolStore.__new__(WarmPoolStore)

        def _boom(*_a, **_k):
            raise RuntimeError("connection refused")

        store._claim_once = _boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            store.claim("sid", "owner")


class TestWarmProtocol:
    def test_warm_frames_are_distinguishable_from_jsonrpc(self):
        initialize = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert wp.decode(initialize) is None
        assert wp.decode(wp.encode(wp.FRAME_PING, seq="a")) is not None

    def test_bind_requires_owner_and_yolo(self):
        """Both are mandatory: on CCI the worker cannot recover either one.

        ``RemoteKimoStorage.load_session_state`` is inert (always None), so a
        bind without them would silently run with ``yolo=False`` and no owner —
        every tool call then waits on an approval the mobile UIs cannot give.
        """
        base = {wp.WARM_KEY: wp.FRAME_BIND, "v": 1, "session_id": str(uuid4())}

        with pytest.raises(wp.WarmProtocolError):
            wp.BindRequest.parse({**base, "yolo": True})  # no owner_id
        with pytest.raises(wp.WarmProtocolError):
            wp.BindRequest.parse({**base, "owner_id": "u"})  # no yolo
        with pytest.raises(wp.WarmProtocolError):
            wp.BindRequest.parse({**base, "owner_id": "u", "yolo": "true"})  # yolo not a bool
        with pytest.raises(wp.WarmProtocolError):
            wp.BindRequest.parse(
                {**base, "session_id": "not-a-uuid", "owner_id": "u", "yolo": True}
            )

        ok = wp.BindRequest.parse({**base, "owner_id": " u ", "yolo": False})
        assert ok.owner_id == "u"
        assert ok.yolo is False

    def test_bind_env_is_allowlisted(self):
        frame = {
            wp.WARM_KEY: wp.FRAME_BIND,
            "v": 1,
            "session_id": str(uuid4()),
            "owner_id": "u",
            "yolo": True,
            "env": {"KIMI_USER_ID": "u", "HTTPS_PROXY": "http://evil", "SUBAGENT": "a"},
        }
        parsed = wp.BindRequest.parse(frame)
        assert parsed.env == {"KIMI_USER_ID": "u", "SUBAGENT": "a"}
