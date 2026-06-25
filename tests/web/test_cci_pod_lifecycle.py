"""gateway-side CCI Pod 生命周期兜底 (hechun-fork-cci).

Covers:
  C. idle 兜底 sweeper —— _reclaim_idle_cci_sessions / _cci_idle_sweeper:
     - idle 超时的 CCI session 被 stop_worker(reason="idle_reclaim")
     - busy / restarting 的 session 不动
     - 未到 TTL 的不动；docker (无 handle) 的不动；mid-spawn (handle=None) 的不动
     - TTL / 间隔可配
  B (app-wiring). KIMI_CCI_RECONCILE_ON_STARTUP=0 时 lifespan 不调用 reconcile_orphans。

All fakes — no real Pods, no real CCI calls.
"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from kimi_cli.web.app import _reclaim_idle_cci_sessions


class _FakeStatus:
    def __init__(self, state: str):
        self.state = state


class _FakeCCIProc:
    """Stand-in for CCISessionProcess: has ``handle`` + last_active_at + status."""

    def __init__(
        self,
        *,
        state: str,
        idle_for: float,
        is_alive: bool = True,
        has_handle: bool = True,
    ):
        self._status = _FakeStatus(state)
        # last_active_at is monotonic; "idle_for" seconds ago.
        self.last_active_at = time.monotonic() - idle_for
        self.is_alive = is_alive
        self.handle = object() if has_handle else None
        self.stopped_with: list[str | None] = []

    @property
    def status(self):
        return self._status

    async def stop_worker(self, *, reason: str | None = None, emit_status: bool = True):
        self.stopped_with.append(reason)


class _FakeDockerProc:
    """Stand-in for a plain SessionProcess (docker/local): NO ``handle`` attr."""

    def __init__(self, *, state: str = "idle", idle_for: float = 10_000):
        self._status = _FakeStatus(state)
        self.last_active_at = time.monotonic() - idle_for
        self.is_alive = True
        self.stopped_with: list[str | None] = []

    @property
    def status(self):
        return self._status

    async def stop_worker(self, *, reason: str | None = None, emit_status: bool = True):
        self.stopped_with.append(reason)


class _FakeRunner:
    def __init__(self, procs: dict):
        self._procs = procs

    def iter_sessions(self):
        return list(self._procs.items())


class TestReclaimIdleCCISessions:
    async def test_idle_cci_session_reclaimed(self):
        sid = uuid4()
        proc = _FakeCCIProc(state="idle", idle_for=1000)
        n = await _reclaim_idle_cci_sessions(_FakeRunner({sid: proc}), idle_ttl_s=900)
        assert n == 1
        assert proc.stopped_with == ["idle_reclaim"]

    async def test_busy_session_not_reclaimed(self):
        sid = uuid4()
        # Busy past the TTL — must NOT be reclaimed (prompt in flight).
        proc = _FakeCCIProc(state="busy", idle_for=10_000)
        n = await _reclaim_idle_cci_sessions(_FakeRunner({sid: proc}), idle_ttl_s=900)
        assert n == 0
        assert proc.stopped_with == []

    async def test_restarting_session_not_reclaimed(self):
        sid = uuid4()
        proc = _FakeCCIProc(state="restarting", idle_for=10_000)
        n = await _reclaim_idle_cci_sessions(_FakeRunner({sid: proc}), idle_ttl_s=900)
        assert n == 0
        assert proc.stopped_with == []

    async def test_idle_within_ttl_not_reclaimed(self):
        sid = uuid4()
        # idle 60s < ttl 900s → keep.
        proc = _FakeCCIProc(state="idle", idle_for=60)
        n = await _reclaim_idle_cci_sessions(_FakeRunner({sid: proc}), idle_ttl_s=900)
        assert n == 0
        assert proc.stopped_with == []

    async def test_ttl_is_configurable(self):
        sid = uuid4()
        proc = _FakeCCIProc(state="idle", idle_for=120)
        # Tighter TTL of 60s → 120s idle now exceeds it → reclaim.
        n = await _reclaim_idle_cci_sessions(_FakeRunner({sid: proc}), idle_ttl_s=60)
        assert n == 1
        assert proc.stopped_with == ["idle_reclaim"]

    async def test_docker_session_without_handle_skipped(self):
        sid = uuid4()
        # Plain SessionProcess (no ``handle``) → never reclaimed by the CCI sweeper.
        proc = _FakeDockerProc(state="idle", idle_for=10_000)
        n = await _reclaim_idle_cci_sessions(_FakeRunner({sid: proc}), idle_ttl_s=900)
        assert n == 0
        assert proc.stopped_with == []

    async def test_mid_spawn_handle_none_skipped(self):
        sid = uuid4()
        # CCI proc but spawn not done yet (handle None) → never reclaim mid-spawn.
        proc = _FakeCCIProc(state="idle", idle_for=10_000, has_handle=False)
        n = await _reclaim_idle_cci_sessions(_FakeRunner({sid: proc}), idle_ttl_s=900)
        assert n == 0
        assert proc.stopped_with == []

    async def test_dead_worker_skipped(self):
        sid = uuid4()
        proc = _FakeCCIProc(state="idle", idle_for=10_000, is_alive=False)
        n = await _reclaim_idle_cci_sessions(_FakeRunner({sid: proc}), idle_ttl_s=900)
        assert n == 0
        assert proc.stopped_with == []

    async def test_one_bad_session_does_not_abort_sweep(self):
        good_sid, bad_sid = uuid4(), uuid4()
        good = _FakeCCIProc(state="idle", idle_for=10_000)
        bad = _FakeCCIProc(state="idle", idle_for=10_000)

        async def boom(*, reason=None, emit_status=True):
            raise RuntimeError("stop failed")

        bad.stop_worker = boom  # type: ignore[assignment]
        runner = _FakeRunner({bad_sid: bad, good_sid: good})
        # Bad session raises but is caught; good session still reclaimed.
        n = await _reclaim_idle_cci_sessions(runner, idle_ttl_s=900)
        assert n == 1
        assert good.stopped_with == ["idle_reclaim"]

    async def test_iter_sessions_failure_returns_zero(self):
        class _BrokenRunner:
            def iter_sessions(self):
                raise RuntimeError("runner exploded")

        # Enumeration failure → 0, no raise.
        assert await _reclaim_idle_cci_sessions(_BrokenRunner(), idle_ttl_s=900) == 0

    async def test_mixed_docker_and_cci(self):
        cci_sid, docker_sid = uuid4(), uuid4()
        cci = _FakeCCIProc(state="idle", idle_for=10_000)
        docker = _FakeDockerProc(state="idle", idle_for=10_000)
        runner = _FakeRunner({cci_sid: cci, docker_sid: docker})
        n = await _reclaim_idle_cci_sessions(runner, idle_ttl_s=900)
        assert n == 1
        assert cci.stopped_with == ["idle_reclaim"]
        assert docker.stopped_with == []


# ── B (app-wiring): reconcile_orphans 启动调用受 env gate 控制 ──────────────────


class _FakeSpawner:
    """Minimal SandboxSpawner with a recording reconcile_orphans + client attr."""

    def __init__(self):
        self.reconcile_calls = 0

        class _Client:
            metrics = None

        self.client = _Client()
        self.namespace = "test-ns"

    async def reconcile_orphans(self) -> int:
        self.reconcile_calls += 1
        return 0


class _FakeCCIRunner:
    def __init__(self, *, spawner, extra_env=None):
        self._spawner = spawner

    def start(self):
        pass

    async def stop(self):
        pass

    def iter_sessions(self):
        return []


def _drive_lifespan(monkeypatch: pytest.MonkeyPatch, *, reconcile_env: str | None):
    """Run create_app's lifespan once with CCI backend + fake spawner/runner.

    Returns the _FakeSpawner so the caller can inspect reconcile_calls.
    """
    from starlette.testclient import TestClient

    import kimi_cli.storage as storage_mod
    import kimi_cli.web.app as app_mod
    import kimi_cli.web.db.database as db_mod
    import kimi_cli.web.runner.cci_process as cci_runner_mod
    import kimi_cli.web.spawner as spawner_mod

    spawner = _FakeSpawner()

    # Force CCI path.
    monkeypatch.setenv("KIMI_SPAWNER_BACKEND", "cci")
    monkeypatch.delenv("KIMI_METRICS_ENABLED", raising=False)
    if reconcile_env is None:
        monkeypatch.delenv("KIMI_CCI_RECONCILE_ON_STARTUP", raising=False)
    else:
        monkeypatch.setenv("KIMI_CCI_RECONCILE_ON_STARTUP", reconcile_env)
    # Keep the sweeper from doing real work during the brief lifespan window:
    # a huge interval means its first sleep never fires before shutdown.
    monkeypatch.setenv("KIMI_CCI_SWEEP_INTERVAL_SECONDS", "100000")

    # Stub heavy / external lifespan deps.
    monkeypatch.setattr(db_mod, "init_db", lambda: None)
    monkeypatch.setattr(storage_mod, "build_storage", lambda: object())
    monkeypatch.setattr(spawner_mod, "build_spawner", lambda: spawner)
    monkeypatch.setattr(cci_runner_mod, "CCIRunner", _FakeCCIRunner)

    app = app_mod.create_app(session_token="t")
    # Entering the TestClient context manager runs lifespan startup; exiting runs
    # shutdown (cancels the sweeper task cleanly).
    with TestClient(app):
        pass
    return spawner


class TestLastActiveAtWiring:
    """SessionProcess.last_active_at: None until first activity, bumped on activity."""

    async def test_none_until_activity_then_bumped(self, monkeypatch: pytest.MonkeyPatch):
        from kimi_cli.web.runner.process import SessionProcess

        proc = SessionProcess(uuid4())
        assert proc.last_active_at is None

        # send_message touches the activity clock even before any worker exists;
        # stub start() + transport so no real subprocess is spawned.
        async def _noop_start(*a, **k):
            return None

        async def _noop_write(_data):
            return None

        monkeypatch.setattr(proc, "start", _noop_start)
        monkeypatch.setattr(proc, "_transport_write_stdin", _noop_write)

        # A non-prompt control message still counts as activity.
        await proc.send_message(
            '{"jsonrpc":"2.0","method":"cancel","id":"c1","params":{}}'
        )
        assert proc.last_active_at is not None


class TestReconcileEnvGate:
    def test_reconcile_called_by_default(self, monkeypatch: pytest.MonkeyPatch):
        spawner = _drive_lifespan(monkeypatch, reconcile_env=None)
        assert spawner.reconcile_calls == 1

    def test_reconcile_skipped_when_env_zero(self, monkeypatch: pytest.MonkeyPatch):
        spawner = _drive_lifespan(monkeypatch, reconcile_env="0")
        assert spawner.reconcile_calls == 0
