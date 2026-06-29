"""Mock integration test: CCISessionProcess ↔ KimoExecStream end-to-end (no Pod).

# hechun-fork-cci

Validates the attach 打通: a mock KimoExecStream (fed fake channel.k8s.io stdout
frames) flows through the SHARED SessionProcess read loop → session broadcast,
and prompts written by the gateway reach the stream's stdin (channel 0). Also
covers clean EOF, error EOF (non-zero exit on error channel), and teardown.

What this CANNOT cover (still needs a real Pod, spec §9-8 ★): the actual exec
101 handshake + real channel.k8s.io framing on the wire. Everything downstream
of ``attach`` (the line below) is exercised here.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from kimi_cli.web.runner.cci_process import CCIRunner, CCISessionProcess
from kimi_cli.web.spawner import SandboxHandle


class FakeExecStream:
    """Stand-in for KimoExecStream with the same transport surface.

    ``stdout_lines`` are handed out one-per-readline; ``error`` + ``returncode``
    model a non-zero exit. ``stdin`` records everything the gateway writes.
    """

    def __init__(self, stdout_lines=None, *, error=b"", returncode=None):
        self._stdout = list(stdout_lines or [])
        self.stdin: list[bytes] = []
        self._error = error
        self._returncode = returncode
        self.closed = False
        self._eof = False

    async def readline(self) -> bytes:
        if self._stdout:
            return self._stdout.pop(0)
        self._eof = True
        return b""

    def at_eof(self) -> bool:
        return self._eof or (not self._stdout and self.closed)

    async def sendall(self, data: bytes) -> None:
        self.stdin.append(data)

    async def recv_stderr(self) -> bytes:
        return self._error

    def read_error(self) -> bytes:
        return b""

    def returncode(self) -> int | None:
        return self._returncode

    async def close(self) -> None:
        self.closed = True
        self._eof = True


class FakeSpawner:
    """Mock SandboxSpawner that hands out a pre-seeded FakeExecStream."""

    def __init__(self, stream: FakeExecStream):
        self._stream = stream
        self.spawned: list = []
        self.stopped: list = []

    async def spawn(self, sid, owner_id, env) -> SandboxHandle:
        self.spawned.append((sid, owner_id, env))
        return SandboxHandle(backend="cci", handle_id=f"kimo-sandbox-{sid}")

    async def attach(self, handle: SandboxHandle):
        return self._stream

    async def stop(self, handle: SandboxHandle) -> None:
        self.stopped.append(handle.handle_id)

    async def healthcheck(self, handle: SandboxHandle) -> bool:
        return True


def _make_proc(stream: FakeExecStream, monkeypatch: pytest.MonkeyPatch) -> tuple:
    """Build a CCISessionProcess wired to a FakeSpawner.

    isinstance(stream, KimoExecStream) is asserted in start(); patch the symbol
    in cci_process to FakeExecStream so the mock passes that gate.
    """
    import kimi_cli.web.runner.cci_process as cci_mod

    monkeypatch.setattr(cci_mod, "KimoExecStream", FakeExecStream)
    # Avoid disk/env IO during _build_sandbox_env.
    monkeypatch.setattr(cci_mod, "_read_owner_id_from_disk", lambda sid: "hechun-1")
    monkeypatch.setattr(cci_mod, "_read_agent_name_from_disk", lambda sid: None)
    monkeypatch.setattr(cci_mod, "get_clean_env", lambda: {})

    spawner = FakeSpawner(stream)
    proc = CCISessionProcess(uuid4(), spawner=spawner)
    return proc, spawner


class TestStdoutToSession:
    async def test_worker_stdout_flows_to_broadcast(self, monkeypatch: pytest.MonkeyPatch):
        # Two JSON-RPC response frames (success-response shape needs no wire
        # deserialization), then clean EOF. Asserts the raw worker stdout line
        # reaches _broadcast — the docker-parity data-flow contract.
        frame1 = json.dumps({"jsonrpc": "2.0", "id": "a", "result": {"ok": 1}})
        frame2 = json.dumps({"jsonrpc": "2.0", "id": "b", "result": {"ok": 2}})
        stream = FakeExecStream(stdout_lines=[(frame1 + "\n").encode(), (frame2 + "\n").encode()])
        proc, _ = _make_proc(stream, monkeypatch)

        broadcasts: list[str] = []

        async def capture(msg: str) -> None:
            broadcasts.append(msg)

        monkeypatch.setattr(proc, "_broadcast", capture)

        # Spawn + attach + run the read loop to EOF.
        await proc.start()
        assert proc._read_task is not None
        await proc._read_task  # loop runs to EOF and returns

        # Both worker stdout frames were broadcast verbatim (minus newline).
        assert frame1 in broadcasts
        assert frame2 in broadcasts

    async def test_spawn_and_attach_invoked(self, monkeypatch: pytest.MonkeyPatch):
        stream = FakeExecStream(stdout_lines=[])
        proc, spawner = _make_proc(stream, monkeypatch)
        await proc.start()
        assert len(spawner.spawned) == 1
        # env forwarded includes session id + resolved owner.
        _sid, owner, env = spawner.spawned[0]
        assert env["KIMI_SESSION_ID"] == str(proc.session_id)
        assert proc.handle is not None
        await proc.stop_worker()


class TestPromptToStdin:
    async def test_prompt_reaches_stream_stdin(self, monkeypatch: pytest.MonkeyPatch):
        stream = FakeExecStream(stdout_lines=[])
        proc, _ = _make_proc(stream, monkeypatch)

        # Don't let the read loop race; stub broadcast.
        async def noop(_m) -> None:
            return None

        monkeypatch.setattr(proc, "_broadcast", noop)

        # _encode_uploaded_files() does disk session lookup; the prompt-framing
        # path is what we test, so stub it to an empty attachment stream.
        async def _no_uploads():
            return
            yield  # pragma: no cover — make this an async generator

        monkeypatch.setattr(proc, "_encode_uploaded_files", _no_uploads)

        prompt = json.dumps(
            {"jsonrpc": "2.0", "method": "prompt", "id": "p1", "params": {"user_input": "hi"}}
        )
        await proc.send_message(prompt)

        # The framed prompt (newline-terminated) reached channel-0 stdin.
        assert len(stream.stdin) == 1
        written = stream.stdin[0].decode()
        assert written.endswith("\n")
        sent = json.loads(written)
        assert sent["method"] == "prompt"
        assert sent["id"] == "p1"
        # prompt id tracked as in-flight → session is busy.
        assert proc.is_busy is True
        await proc.stop_worker()


class TestEofAndErrorPaths:
    async def test_clean_eof_when_expecting_exit_no_error_broadcast(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        stream = FakeExecStream(stdout_lines=[])
        proc, _ = _make_proc(stream, monkeypatch)
        broadcasts: list[str] = []

        async def capture(msg: str) -> None:
            broadcasts.append(msg)

        monkeypatch.setattr(proc, "_broadcast", capture)
        await proc.start()
        proc._expecting_exit = True  # graceful stop expected
        await proc._read_task
        # No error response broadcast on an expected exit.
        assert not any('"error"' in b for b in broadcasts)

    async def test_unexpected_eof_broadcasts_error_with_returncode(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Worker dies immediately: EOF + non-zero exit on error channel.
        stream = FakeExecStream(stdout_lines=[], error=b"boom", returncode=137)
        proc, _ = _make_proc(stream, monkeypatch)
        broadcasts: list[str] = []

        async def capture(msg: str) -> None:
            broadcasts.append(msg)

        monkeypatch.setattr(proc, "_broadcast", capture)
        await proc.start()
        proc._expecting_exit = False
        await proc._read_task

        # An error RESPONSE (top-level "error" object) carrying the worker
        # stderr + exit code was broadcast (distinct from the error status event).
        err_responses = []
        for b in broadcasts:
            try:
                parsed = json.loads(b)
            except ValueError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict) and "code" in parsed["error"]:
                err_responses.append(parsed)
        assert err_responses, "expected a JSONRPCErrorResponse broadcast on unexpected EOF"
        payload = err_responses[-1]
        assert payload["error"]["code"] == 137
        assert "boom" in payload["error"]["message"]
        assert proc.status.state == "error"

    async def test_stop_closes_stream_and_deletes_pod(self, monkeypatch: pytest.MonkeyPatch):
        stream = FakeExecStream(stdout_lines=[])
        proc, spawner = _make_proc(stream, monkeypatch)
        await proc.start()
        handle_id = proc.handle.handle_id  # type: ignore[union-attr]
        await proc.stop_worker()
        assert stream.closed is True
        assert handle_id in spawner.stopped
        assert proc.handle is None


class TestWorkerExitDeletesPod:
    """hechun-fork-cci 主修复: an unexpected worker exit (any exit code, or a
    read-loop error) deletes the now-worthless keepalive Pod immediately, rather
    than leaking it Running forever (session goes ``error`` but Pod stays up)."""

    async def test_unexpected_exit_deletes_pod(self, monkeypatch: pytest.MonkeyPatch):
        # Generic non-zero exit (NOT the agent-load code) → Pod must be deleted.
        stream = FakeExecStream(stdout_lines=[], error=b"boom", returncode=1)
        proc, spawner = _make_proc(stream, monkeypatch)

        async def noop(_m) -> None:
            return None

        monkeypatch.setattr(proc, "_broadcast", noop)
        await proc.start()
        handle_id = proc.handle.handle_id  # type: ignore[union-attr]
        proc._expecting_exit = False
        await proc._read_task

        # spawner.stop was invoked with the dead Pod, handle cleared, stream closed.
        assert handle_id in spawner.stopped
        assert proc.handle is None
        assert stream.closed is True
        # And the session is no longer "alive" (stream gone).
        assert proc.is_alive is False

    async def test_clean_exit_when_expecting_exit_does_not_double_delete(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # A graceful stop_worker() already deleted the Pod; a subsequent clean EOF
        # (expecting_exit=True) must NOT call spawner.stop again (handle already None).
        stream = FakeExecStream(stdout_lines=[])
        proc, spawner = _make_proc(stream, monkeypatch)

        async def noop(_m) -> None:
            return None

        monkeypatch.setattr(proc, "_broadcast", noop)
        await proc.start()
        await proc.stop_worker()  # deletes the Pod once (expected path)
        assert len(spawner.stopped) == 1
        # The read loop already ended on cancel; re-running it is a no-op. The clean
        # EOF branch (expecting_exit) never reaches _on_worker_exit, so no 2nd delete.
        assert len(spawner.stopped) == 1

    async def test_read_loop_error_deletes_pod(self, monkeypatch: pytest.MonkeyPatch):
        # The read loop blows up (e.g. exec WS dropped mid-stream) → the read_loop_error
        # branch must also release the Pod via _on_worker_exit (the leak this branch
        # was missing). Drive _read_loop directly with a readline that raises, so the
        # generic-exception path is hit deterministically (no EOF race).
        stream = FakeExecStream(stdout_lines=[])

        async def boom() -> bytes:
            raise RuntimeError("exec websocket dropped")

        stream.readline = boom  # type: ignore[method-assign]
        proc, spawner = _make_proc(stream, monkeypatch)

        async def noop(_m) -> None:
            return None

        monkeypatch.setattr(proc, "_broadcast", noop)

        # Manually wire the spawn result without starting the auto read task.
        from kimi_cli.web.spawner import SandboxHandle

        handle = SandboxHandle(backend="cci", handle_id="kimo-sandbox-x")
        proc._handle = handle
        proc._exec_stream = stream

        await proc._read_loop()

        assert "kimo-sandbox-x" in spawner.stopped
        assert proc.handle is None
        assert proc.status.state == "error"

    async def test_agent_load_failure_still_deletes_pod(self, monkeypatch: pytest.MonkeyPatch):
        # The agent-load-failure exit code path ALSO deletes the Pod now (it used to
        # only record the metric + broadcast, leaving the Pod Running).
        from kimi_cli.web.runner.worker import AGENT_LOAD_FAILURE_EXIT_CODE

        stream = FakeExecStream(
            stdout_lines=[],
            error=b"required agent could not be loaded",
            returncode=AGENT_LOAD_FAILURE_EXIT_CODE,
        )
        proc, spawner = _make_proc(stream, monkeypatch)

        async def noop(_m) -> None:
            return None

        monkeypatch.setattr(proc, "_broadcast", noop)
        await proc.start()
        handle_id = proc.handle.handle_id  # type: ignore[union-attr]
        proc._expecting_exit = False
        await proc._read_task

        assert handle_id in spawner.stopped
        assert proc.handle is None


class TestCCIRunner:
    async def test_get_or_create_returns_cci_process(self, monkeypatch: pytest.MonkeyPatch):
        stream = FakeExecStream(stdout_lines=[])
        spawner = FakeSpawner(stream)
        runner = CCIRunner(spawner=spawner)
        sid = uuid4()
        proc = await runner.get_or_create_session(sid)
        assert isinstance(proc, CCISessionProcess)
        # idempotent
        assert (await runner.get_or_create_session(sid)) is proc
        assert runner.get_session(sid) is proc


class TestRequireAgentEnvInjection:
    """hechun-fork-cci: when the gateway forwards an agent name (SUBAGENT) it must
    also flip KIMI_REQUIRE_AGENT=1 so the worker fails fast instead of silently
    falling back to the default agent."""

    def _build_env(self, monkeypatch: pytest.MonkeyPatch, *, agent_name):
        import kimi_cli.web.runner.cci_process as cci_mod

        monkeypatch.setattr(cci_mod, "_read_owner_id_from_disk", lambda sid: "hechun-1")
        monkeypatch.setattr(cci_mod, "_read_agent_name_from_disk", lambda sid: agent_name)
        monkeypatch.setattr(cci_mod, "get_clean_env", lambda: {})
        spawner = FakeSpawner(FakeExecStream(stdout_lines=[]))
        proc = CCISessionProcess(uuid4(), spawner=spawner)
        return proc._build_sandbox_env()

    def test_require_agent_injected_when_subagent_set(self, monkeypatch: pytest.MonkeyPatch):
        env = self._build_env(monkeypatch, agent_name="diabetes-expert")
        assert env["SUBAGENT"] == "diabetes-expert"
        assert env["KIMI_REQUIRE_AGENT"] == "1"

    def test_require_agent_absent_when_no_subagent(self, monkeypatch: pytest.MonkeyPatch):
        env = self._build_env(monkeypatch, agent_name=None)
        assert "SUBAGENT" not in env
        assert "KIMI_REQUIRE_AGENT" not in env


class _MetricsSpawner(FakeSpawner):
    """FakeSpawner that also carries a metrics sink (mirrors CCISpawner.metrics)."""

    def __init__(self, stream: FakeExecStream):
        super().__init__(stream)

        class _Metrics:
            def __init__(self):
                self.agent_load_failures: list[str] = []

            def record_agent_load_failure(self, *, reason: str) -> None:
                self.agent_load_failures.append(reason)

        self.metrics = _Metrics()


class TestAgentLoadFailureExitCode:
    """Gateway recognises AGENT_LOAD_FAILURE_EXIT_CODE on worker EOF → records
    the metric + broadcasts a clear client-facing error."""

    async def test_exit_code_42_records_metric_and_broadcasts(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from kimi_cli.web.runner.worker import AGENT_LOAD_FAILURE_EXIT_CODE

        # Worker died with the agent-load failure exit code; stderr carries the
        # human message (no wire stdout line in this scenario).
        stream = FakeExecStream(
            stdout_lines=[],
            error=b"required agent could not be loaded",
            returncode=AGENT_LOAD_FAILURE_EXIT_CODE,
        )
        import kimi_cli.web.runner.cci_process as cci_mod

        monkeypatch.setattr(cci_mod, "KimoExecStream", FakeExecStream)
        monkeypatch.setattr(cci_mod, "_read_owner_id_from_disk", lambda sid: "hechun-1")
        monkeypatch.setattr(cci_mod, "_read_agent_name_from_disk", lambda sid: None)
        monkeypatch.setattr(cci_mod, "get_clean_env", lambda: {})

        spawner = _MetricsSpawner(stream)
        proc = CCISessionProcess(uuid4(), spawner=spawner)

        broadcasts: list[str] = []

        async def capture(msg: str) -> None:
            broadcasts.append(msg)

        monkeypatch.setattr(proc, "_broadcast", capture)
        await proc.start()
        proc._expecting_exit = False
        await proc._read_task

        # Metric recorded exactly once with the agent_required_missing reason.
        assert spawner.metrics.agent_load_failures == ["agent_required_missing"]

        # A clear, client-visible error was broadcast carrying code 42 + reason.
        agent_errors = []
        for b in broadcasts:
            try:
                parsed = json.loads(b)
            except ValueError:
                continue
            err = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(err, dict) and err.get("code") == AGENT_LOAD_FAILURE_EXIT_CODE:
                agent_errors.append(parsed)
        assert agent_errors, "expected an agent-load-failure error broadcast"
        # The dedicated _on_worker_exit broadcast stamps the reason + clear text.
        explicit = [
            e
            for e in agent_errors
            if isinstance(e["error"].get("data"), dict)
            and e["error"]["data"].get("reason") == "agent_required_missing"
        ]
        assert explicit, "expected the explicit reason-stamped agent-load error"
        assert "Agent load failed" in explicit[-1]["error"]["message"]

    async def test_non_42_exit_does_not_record_agent_metric(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # A generic crash (different exit code) must NOT bump the agent metric.
        stream = FakeExecStream(stdout_lines=[], error=b"boom", returncode=137)
        import kimi_cli.web.runner.cci_process as cci_mod

        monkeypatch.setattr(cci_mod, "KimoExecStream", FakeExecStream)
        monkeypatch.setattr(cci_mod, "_read_owner_id_from_disk", lambda sid: "hechun-1")
        monkeypatch.setattr(cci_mod, "_read_agent_name_from_disk", lambda sid: None)
        monkeypatch.setattr(cci_mod, "get_clean_env", lambda: {})

        spawner = _MetricsSpawner(stream)
        proc = CCISessionProcess(uuid4(), spawner=spawner)

        async def noop(_m) -> None:
            return None

        monkeypatch.setattr(proc, "_broadcast", noop)
        await proc.start()
        proc._expecting_exit = False
        await proc._read_task

        assert spawner.metrics.agent_load_failures == []
