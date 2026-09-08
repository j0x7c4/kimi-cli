"""Session exec-stream keepalive — the other half of "whoever holds a Pod keeps it alive".

# hechun-fork-cci

2026-09-08, four samples (prod ×3 + test ×1): the CCI exec WebSocket is closed by
the far side after **exactly 5 minutes** of no traffic — an *idle* timer, not a
connection age limit (prod 09-06 died 5m00s after its last frame, not after
connecting). Nothing on our side pinged: ``create_connection`` is followed by
``settimeout(None)`` and ``cci_exec.py`` had no ping/pong anywhere.

User-visible consequence: pause for five minutes, send a message → it lands in a
closed socket, produces no turn at all, and because an unfinished turn is never
persisted it also vanishes from history; the session is then reclaimed in
``error`` and every following message is blocked behind it.

The warm pool already kept *pooled* Pods alive on a 60s cadence. The Pod a user
is actually talking to had nothing — so idle pooled Pods lived all day while
live sessions died in five minutes.
"""

from __future__ import annotations

import asyncio

from kimi_cli.web.runner import cci_process as cp


class FakeStream:
    """Records keepalive pokes; can be made to fail like a dead stream."""

    def __init__(self, fail: bool = False) -> None:
        self.pokes = 0
        self.writes: list[bytes] = []
        self._fail = fail

    async def keepalive(self) -> None:
        self.pokes += 1
        if self._fail:
            raise ConnectionResetError("stream is gone")

    async def sendall(self, data: bytes) -> None:
        self.writes.append(data)


class _Proc:
    """Minimal stand-in exposing only what the keepalive helpers touch.

    Constructing a real ``CCISessionProcess`` would drag in a spawner, an event
    emitter and a session registry — none of which this behaviour depends on.
    The methods under test are bound from the real class, so a regression in
    them is still caught.
    """

    def __init__(self, stream: FakeStream | None, interval: float = 0.01) -> None:
        self.session_id = "sid-1"
        self._exec_stream = stream
        self._keepalive_interval_s = interval
        self._keepalive_task: asyncio.Task | None = None

    _start_keepalive = cp.CCISessionProcess._start_keepalive
    _stop_keepalive = cp.CCISessionProcess._stop_keepalive
    _keepalive_loop = cp.CCISessionProcess._keepalive_loop


async def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


class TestKeepaliveRuns:
    async def test_the_stream_is_poked_repeatedly(self):
        """The whole point: traffic keeps flowing while the user is idle."""
        stream = FakeStream()
        proc = _Proc(stream)
        proc._start_keepalive()
        try:
            assert await _wait_until(lambda: stream.pokes >= 3), (
                f"保活没有周期性戳流，pokes={stream.pokes}"
            )
        finally:
            await proc._stop_keepalive()

    async def test_stopping_cancels_it(self):
        stream = FakeStream()
        proc = _Proc(stream)
        proc._start_keepalive()
        assert await _wait_until(lambda: stream.pokes >= 1)
        task = proc._keepalive_task
        await proc._stop_keepalive()
        assert task is not None and task.cancelled() or task.done()
        assert proc._keepalive_task is None

        after_stop = stream.pokes
        await asyncio.sleep(0.05)
        assert stream.pokes == after_stop, "停掉之后不该再戳"

    async def test_starting_twice_does_not_stack_loops(self):
        """A restart path calls start again; two loops would double the traffic."""
        stream = FakeStream()
        proc = _Proc(stream)
        proc._start_keepalive()
        first = proc._keepalive_task
        proc._start_keepalive()
        try:
            assert proc._keepalive_task is first
        finally:
            await proc._stop_keepalive()

    async def test_zero_interval_disables_it(self):
        """Escape hatch: 0 means off, and off must mean no task at all."""
        stream = FakeStream()
        proc = _Proc(stream, interval=0)
        proc._start_keepalive()
        assert proc._keepalive_task is None
        await asyncio.sleep(0.05)
        assert stream.pokes == 0


class TestKeepaliveIsHarmless:
    async def test_a_dead_stream_does_not_kill_the_loop(self):
        """The read loop owns stream death; a failing poke must not raise or stop."""
        stream = FakeStream(fail=True)
        proc = _Proc(stream)
        proc._start_keepalive()
        try:
            assert await _wait_until(lambda: stream.pokes >= 3), (
                "第一次戳失败后循环就停了 —— 流暂时不可用不该让保活永久退出"
            )
            assert not proc._keepalive_task.done()
        finally:
            await proc._stop_keepalive()

    async def test_missing_stream_is_skipped_not_crashed(self):
        """Between teardown and restart ``_exec_stream`` is None for a while."""
        proc = _Proc(None)
        proc._start_keepalive()
        try:
            await asyncio.sleep(0.05)
            assert not proc._keepalive_task.done()
        finally:
            await proc._stop_keepalive()


class TestEnvOverride:
    def test_default_is_60s(self, monkeypatch):
        monkeypatch.delenv(cp._ENV_SESSION_KEEPALIVE, raising=False)
        assert cp._keepalive_interval_from_env() == 60

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv(cp._ENV_SESSION_KEEPALIVE, "15")
        assert cp._keepalive_interval_from_env() == 15

    def test_zero_is_honoured_as_off(self, monkeypatch):
        monkeypatch.setenv(cp._ENV_SESSION_KEEPALIVE, "0")
        assert cp._keepalive_interval_from_env() == 0

    def test_garbage_falls_back_to_default(self, monkeypatch):
        """A typo must not silently disable the thing that keeps sessions alive."""
        monkeypatch.setenv(cp._ENV_SESSION_KEEPALIVE, "sixty")
        assert cp._keepalive_interval_from_env() == 60


class TestWiring:
    """保活被**接上**了没有 —— 循环本身正确但没人启动，等于没修。

    这里用源码断言（与 ``TestFrozenClaimSQL`` 守认领 SQL 同一范式）：真正驱动
    ``CCISessionProcess.start()`` 需要 spawner / 事件发射器 / 会话注册表整套夹具，
    而要守的不变量其实很小 —— 「两条启动路径都起保活、停止路径取消保活」。
    2026-09-08 首版测试漏了这条：把 start() 里的调用删掉，全部用例照样绿。
    """

    def test_start_arms_the_keepalive_on_both_paths(self):
        import inspect

        src = inspect.getsource(cp.CCISessionProcess.start)
        # 两条路径：①已 alive 的提前返回路径（重连/复用）②真正拉起 Pod 的主路径。
        # 少接一条，那条路径上的会话就仍然会在 5 分钟后断掉。
        assert src.count("_start_keepalive()") == 2, (
            f"start() 应在两条路径上都起保活，实际 {src.count('_start_keepalive()')} 处"
        )

    def test_stop_worker_cancels_the_keepalive(self):
        import inspect

        src = inspect.getsource(cp.CCISessionProcess.stop_worker)
        assert "_stop_keepalive()" in src, "停 worker 必须取消保活，否则任务泄漏到下一个 Pod"


class TestExecStreamPoke:
    """The poke itself: real frame on the wire, zero bytes to the worker."""

    def test_keepalive_writes_an_empty_stdin_frame(self):
        from kimi_cli.web.spawner.cci_exec import KimoExecStream

        class _WS:
            def __init__(self):
                self.stdin_writes: list[str] = []

            def write_stdin(self, data):
                self.stdin_writes.append(data)

        stream = KimoExecStream.__new__(KimoExecStream)
        ws = _WS()
        stream._ws = ws

        asyncio.run(stream.keepalive())

        # Empty payload → ``write_channel`` still sends ``chr(0)`` (a real frame,
        # so the idle timer resets) while the worker's stdin receives 0 bytes —
        # invisible to JSON-RPC framing. Sending anything non-empty here would
        # corrupt the wire of a bound worker.
        assert ws.stdin_writes == [""]
