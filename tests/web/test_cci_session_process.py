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
    monkeypatch.setattr(cci_mod, "_read_subagent_from_disk", lambda sid: None)
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
