"""Worker runtime diagnostics — log forwarding + gateway-side wire signals.

# hechun-fork-cci

Context for anyone tempted to loosen these: the Pod is a black box in
production. ``enable_logging`` installs a file sink only and dup2's fd 2 into
it, so every runtime warning inside the Pod (MCP call timed out, MCP tool
returned error, approval timed out) is invisible from the gateway. On
2026-09-08 that cost a whole diagnosis: a session sat at ``state=busy`` for five
minutes, the worker never said a word, and the manual ``/stop`` needed to
unblock the user destroyed the evidence.
"""

from __future__ import annotations

import json

import pytest

from kimi_cli.web.runner import worker_diag as wd


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (wd.ENV_TRACE, wd.ENV_TRACE_LEVEL, "KIMI_SESSION_ID", "KIMI_USER_ID"):
        monkeypatch.delenv(var, raising=False)
    # Each test gets a fresh limiter: the cap is fixed-window and shared.
    monkeypatch.setattr(wd, "_limiter", wd._RateLimiter())


class _Stdout:
    """Reads back what was written to the real ``sys.stdout`` (via capsys).

    Deliberately not a monkeypatched ``sys.stdout``: that fights pytest's own
    capture and passes only under ``-s``. Going through capsys means these
    tests exercise the production path — a frame really is a line on stdout.
    """

    def __init__(self, capsys) -> None:
        self._capsys = capsys
        self._seen: list[str] = []

    def frames(self) -> list[dict]:
        out = self._capsys.readouterr().out
        self._seen.extend(line for line in out.splitlines() if line.strip())
        return [json.loads(line) for line in self._seen]


@pytest.fixture
def stdout(capsys) -> _Stdout:
    return _Stdout(capsys)


class TestGate:
    def test_disabled_by_default_writes_nothing(self, stdout):
        """Off unless asked: a forwarder on by default would ship Pod text everywhere."""
        wd.emit("worker_log", text="boom")
        assert stdout.frames() == []

    def test_enabled_writes_one_json_line(self, stdout, monkeypatch):
        monkeypatch.setenv(wd.ENV_TRACE, "1")
        wd.emit("worker_log", text="boom")
        frames = stdout.frames()
        assert len(frames) == 1
        assert frames[0][wd.FRAME_KEY] == "worker_log"
        assert frames[0]["text"] == "boom"


class TestIdentity:
    """Every frame carries sid + uid — a Pod log without them is nearly useless."""

    def test_sid_and_uid_are_stamped(self, stdout, monkeypatch):
        monkeypatch.setenv(wd.ENV_TRACE, "1")
        monkeypatch.setenv("KIMI_SESSION_ID", "sess-42")
        monkeypatch.setenv("KIMI_USER_ID", "hechun-7")
        wd.emit("worker_log", text="x")
        frame = stdout.frames()[0]
        assert frame["sid"] == "sess-42"
        assert frame["uid"] == "hechun-7"

    def test_identity_is_read_at_emit_time_not_cached(self, stdout, monkeypatch):
        """🔴 The warm-pool path starts identity-free and binds later.

        Caching at import (or at first emit) would stamp every frame of a
        warm-pooled session as anonymous — precisely the sessions we most need
        to trace, since they are the ones that go through bind.
        """
        monkeypatch.setenv(wd.ENV_TRACE, "1")
        wd.emit("worker_log", text="pre-bind")
        monkeypatch.setenv("KIMI_SESSION_ID", "sess-9")
        monkeypatch.setenv("KIMI_USER_ID", "hechun-9")
        wd.emit("worker_log", text="post-bind")

        pre, post = stdout.frames()
        assert "sid" not in pre and "uid" not in pre  # no user exists yet — say so
        assert post["sid"] == "sess-9"
        assert post["uid"] == "hechun-9"


class TestBounds:
    def test_long_text_is_truncated_with_a_visible_marker(self, stdout, monkeypatch):
        monkeypatch.setenv(wd.ENV_TRACE, "1")
        wd.emit("worker_log", text="x" * (wd.MAX_TEXT + 500))
        text = stdout.frames()[0]["text"]
        assert len(text) < wd.MAX_TEXT + 100
        assert "+500 chars" in text  # a cut must announce itself

    def test_rate_cap_drops_and_then_reports_the_gap(self, stdout, monkeypatch):
        """A silent cap would read as 'nothing happened' — the worst failure mode here."""
        monkeypatch.setenv(wd.ENV_TRACE, "1")
        monkeypatch.setattr(wd, "_limiter", wd._RateLimiter(limit=3, window_s=1000.0))
        for i in range(10):
            wd.emit("worker_log", text=f"m{i}")
        frames = stdout.frames()
        assert len(frames) == 3  # capped

        # New window → the next frame carries how many were dropped.
        wd._limiter._window_start -= 2000.0
        wd.emit("worker_log", text="after")
        assert stdout.frames()[-1]["dropped"] == 7

    def test_emit_never_raises_on_unserialisable_payload(self, stdout, monkeypatch):
        monkeypatch.setenv(wd.ENV_TRACE, "1")

        class Weird:
            def __repr__(self) -> str:
                return "<weird>"

        wd.emit("worker_log", obj=Weird())  # must not raise
        assert stdout.frames()[0]["obj"] == "<weird>"


class TestLogForwarding:
    class _FakeLogger:
        def __init__(self) -> None:
            self.sinks: list[tuple] = []

        def add(self, sink, **kwargs):
            self.sinks.append((sink, kwargs))
            return len(self.sinks)

    class _Msg:
        def __init__(self, level="WARNING", message="mcp timeout", exc=None):
            self.record = {
                "level": type("L", (), {"name": level})(),
                "name": "kimi_cli.soul.toolset",
                "function": "__call__",
                "line": 673,
                "message": message,
                "exception": exc,
            }

    def test_not_installed_when_disabled(self):
        log = self._FakeLogger()
        assert wd.install_log_forwarding(log) is False
        assert log.sinks == []

    def test_installed_at_warning_by_default(self, monkeypatch):
        monkeypatch.setenv(wd.ENV_TRACE, "1")
        log = self._FakeLogger()
        assert wd.install_log_forwarding(log) is True
        assert log.sinks[0][1]["level"] == "WARNING"

    def test_level_is_configurable(self, monkeypatch):
        monkeypatch.setenv(wd.ENV_TRACE, "1")
        monkeypatch.setenv(wd.ENV_TRACE_LEVEL, "info")
        log = self._FakeLogger()
        wd.install_log_forwarding(log)
        assert log.sinks[0][1]["level"] == "INFO"

    def test_sink_forwards_the_record_with_source_location(self, stdout, monkeypatch):
        """The forwarded line must be traceable to source without opening the Pod."""
        monkeypatch.setenv(wd.ENV_TRACE, "1")
        monkeypatch.setenv("KIMI_USER_ID", "hechun-5")
        log = self._FakeLogger()
        wd.install_log_forwarding(log)
        sink = log.sinks[0][0]

        sink(self._Msg(message="MCP tool call timed out: get_glucose"))

        frame = stdout.frames()[0]
        assert frame[wd.FRAME_KEY] == "worker_log"
        assert frame["level"] == "WARNING"
        assert frame["text"] == "MCP tool call timed out: get_glucose"
        assert frame["where"] == "kimi_cli.soul.toolset:__call__:673"
        assert frame["uid"] == "hechun-5"

    def test_a_logging_sink_cannot_feed_itself(self, stdout, monkeypatch):
        """Re-entrancy guard: emit() must not be re-entered from inside emit()."""
        monkeypatch.setenv(wd.ENV_TRACE, "1")
        depth = {"n": 0}
        real_emit_line = wd._emit_line

        def reentrant(line: str) -> None:
            depth["n"] += 1
            if depth["n"] < 3:
                # A sink that logged (or a write hook that did) would land here.
                wd.emit("worker_log", text="recursive")
            real_emit_line(line)

        monkeypatch.setattr(wd, "_emit_line", reentrant)
        wd.emit("worker_log", text="outer")
        assert len(stdout.frames()) == 1
        assert depth["n"] == 1  # re-entry was refused, not merely deduplicated


class TestWarmPhaseDiagVisibility:
    """预热阶段的诊断帧不能被丢掉 —— 那正是准备失败的高发区。

    在 Pod 被认领之前，读这条流的是 warmpool 的握手读者而**不是**
    ``CCISessionProcess._read_loop``（后者才认识 kimo_diag）。原实现把所有
    非 warm 行按 debug 丢弃，于是 ``_fetch_sandbox_assets`` 那类「记了日志但
    不抛异常」的失败在 gateway 侧完全不可见 —— Pod 能起来、却服务不了任何
    认领。2026-09-08 验证日志回流功能本身时发现：帧确实发出来了，在这里被
    静默吃掉。
    """

    def test_diag_frame_is_recognised(self):
        from kimi_cli.web.warmpool.manager import _warm_diag_frame

        frame = _warm_diag_frame(
            b'{"kimo_diag": "worker_log", "level": "WARNING", "text": "assets missing"}\n'
        )
        assert frame is not None
        assert frame["kimo_diag"] == "worker_log"
        assert frame["text"] == "assets missing"

    def test_non_diag_lines_are_not_mistaken_for_frames(self):
        from kimi_cli.web.warmpool.manager import _warm_diag_frame

        assert _warm_diag_frame(b"not json at all\n") is None
        assert _warm_diag_frame(b'{"warm": "ready"}\n') is None  # a warm frame, not diag
        assert _warm_diag_frame(b'{"kimo_diag": 42}\n') is None  # key must be a str
        assert _warm_diag_frame(b"[1,2,3]\n") is None  # JSON but not an object

    async def test_await_frame_logs_the_diag_instead_of_dropping_it(self, monkeypatch):
        """🔴 The behaviour, not just the helper.

        Asserting only that ``_warm_diag_frame`` decodes correctly would pass even
        if ``_await_frame`` never called it — which is exactly the state that hid
        this gap in the first place. So drive the real read loop and assert the
        frame reached the log.
        """
        import asyncio

        from kimi_cli.web.runner import warm_protocol as wp
        from kimi_cli.web.warmpool import manager as mgr_mod

        logged: list[tuple[str, dict]] = []

        class _StubLogger:
            def info(self, msg, **kw):
                logged.append((msg, kw))

            def debug(self, msg, **kw):
                logged.append(("DEBUG:" + msg, kw))

            def error(self, msg, **kw):
                logged.append(("ERROR:" + msg, kw))

        monkeypatch.setattr(mgr_mod, "logger", _StubLogger())

        class _Stream:
            def __init__(self, lines):
                self._lines = list(lines)

            async def readline(self):
                await asyncio.sleep(0)
                return self._lines.pop(0) if self._lines else b""

        stream = _Stream(
            [
                b'{"kimo_diag": "worker_log", "level": "WARNING", "text": "assets missing"}\n',
                wp.encode(wp.FRAME_READY, ok=True).encode("utf-8"),
            ]
        )

        # ``_await_frame`` touches no instance state, so a bare object is a
        # sufficient ``self`` here.
        frame = await mgr_mod.WarmPoolManager._await_frame(
            object(), stream, wp.FRAME_READY, timeout=5.0
        )

        assert frame is not None and frame.get(wp.WARM_KEY) == wp.FRAME_READY
        diag_logs = [kw for msg, kw in logged if "warm-diag" in msg]
        assert len(diag_logs) == 1, f"诊断帧没有被记录，实际日志: {logged}"
        assert diag_logs[0]["payload"]["text"] == "assets missing"


class TestGatewayWireSignal:
    """The gateway logs *what the worker is waiting on* — requests only, never events."""

    def test_approval_request_is_called_out_as_blocking(self):
        from kimi_cli.web.runner.process import wire_signal
        from kimi_cli.wire.types import ApprovalRequest

        sig = wire_signal(
            ApprovalRequest(
                id="a1",
                tool_call_id="tc1",
                sender="mcp",
                action="mcp:get_glucose",
                description="Call MCP tool",
                source_kind="foreground_turn",
                source_id="tc1",
                display=[],
            )
        )
        assert sig is not None
        assert "approval_request" in sig
        assert "mcp:get_glucose" in sig
        # 🔴 soul/approval.py waits with timeout=None — say so, loudly.
        assert "BLOCKED" in sig

    def test_tool_call_request_names_the_tool(self):
        from kimi_cli.web.runner.process import wire_signal
        from kimi_cli.wire.types import ToolCallRequest

        sig = wire_signal(ToolCallRequest(id="t1", name="Bash", arguments='{"cmd":"ls"}'))
        assert sig is not None and "Bash" in sig
        # Arguments may carry user content / secrets — they must NOT be logged.
        assert "ls" not in sig

    def test_unknown_params_stay_silent(self):
        from kimi_cli.web.runner.process import wire_signal

        assert wire_signal(object()) is None
        assert wire_signal(None) is None
