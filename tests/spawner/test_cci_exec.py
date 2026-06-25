"""Unit tests for KimoExecStream: channel.k8s.io framing + attach_socket parity.

# hechun-fork-cci

Mock WSClient — no real WebSocket. Verifies the exec URL shape, channel framing
helpers, and the docker attach_socket()-equivalent sendall/recv API.
"""

from __future__ import annotations

import pytest

from kimi_cli.web.spawner.cci_auth import TokenProvider
from kimi_cli.web.spawner.cci_exec import (
    ERROR_CHANNEL,
    STDERR_CHANNEL,
    STDIN_CHANNEL,
    STDOUT_CHANNEL,
    KimoExecStream,
    build_exec_url,
    demux_frame,
    frame_stdin,
)


class TestFraming:
    def test_demux_stdout_frame(self):
        frame = bytes([STDOUT_CHANNEL]) + b"hello"
        ch, payload = demux_frame(frame)
        assert ch == STDOUT_CHANNEL
        assert payload == b"hello"

    def test_demux_stderr_frame(self):
        frame = bytes([STDERR_CHANNEL]) + b"oops"
        ch, payload = demux_frame(frame)
        assert ch == STDERR_CHANNEL
        assert payload == b"oops"

    def test_demux_error_channel(self):
        ch, payload = demux_frame(bytes([ERROR_CHANNEL]) + b'{"status":"Failure"}')
        assert ch == ERROR_CHANNEL

    def test_demux_empty_frame(self):
        assert demux_frame(b"") == (-1, b"")

    def test_frame_stdin_prefixes_channel_zero(self):
        framed = frame_stdin(b"abc")
        assert framed[0] == STDIN_CHANNEL
        assert framed[1:] == b"abc"


class TestExecUrl:
    def test_url_has_cci_v2_exec_path_and_params(self):
        url = build_exec_url(
            "cci.cn-x.myhuaweicloud.com", "hechun-prod", "kimo-sandbox-1", ["/bin/sh"]
        )
        assert url.startswith(
            "wss://cci.cn-x.myhuaweicloud.com/apis/cci/v2/namespaces/hechun-prod/pods/kimo-sandbox-1/exec?"
        )
        assert "command=%2Fbin%2Fsh" in url
        assert "stdin=true" in url
        assert "stdout=true" in url
        assert "stderr=true" in url
        assert "tty=false" in url

    def test_multiple_command_args_repeat_param(self):
        url = build_exec_url("h", "ns", "p", ["python", "-m", "worker"])
        assert url.count("command=") == 3


class _FakeWS:
    def __init__(self, stdout_queue=None, *, open_after_drain=True):
        self.stdin_writes: list = []
        self.stdout_queue: list = list(stdout_queue) if stdout_queue is not None else ["line1", "line2"]
        self.closed = False
        self._open_after_drain = open_after_drain
        self.returncode = None
        self.error_queue: list = []

    def write_stdin(self, data):
        self.stdin_writes.append(data)

    def read_stdout(self, timeout=None):
        return self.stdout_queue.pop(0) if self.stdout_queue else None

    def read_stderr(self, timeout=0):
        return ""

    def read_channel(self, channel, timeout=0):
        return self.error_queue.pop(0) if self.error_queue else None

    def is_open(self):
        # Open until drained when open_after_drain is False (simulates EOF).
        if self.stdout_queue:
            return True
        return self._open_after_drain

    def close(self):
        self.closed = True


class TestKimoExecStreamApi:
    @pytest.fixture
    def stream(self, monkeypatch: pytest.MonkeyPatch) -> KimoExecStream:
        s = KimoExecStream()
        fake_ws = _FakeWS()
        # Bypass real WSClient: stub _open_ws.
        monkeypatch.setattr(s, "_open_ws", lambda url, token: fake_ws)
        s._fake_ws = fake_ws  # type: ignore[attr-defined]
        return s

    async def test_connect_then_sendall_recv(
        self, stream: KimoExecStream, monkeypatch: pytest.MonkeyPatch
    ):
        tp = TokenProvider("ak", "sk", "cn-x")

        async def fake_fetch():
            return "tok-xyz"

        monkeypatch.setattr(tp, "_fetch_token", fake_fetch)

        await stream.connect("ep", "ns", "pod", tp)
        await stream.sendall(b'{"jsonrpc":"2.0"}\n')
        assert stream._fake_ws.stdin_writes == ['{"jsonrpc":"2.0"}\n']  # type: ignore[attr-defined]

        out = await stream.recv()
        assert out == b"line1"
        out2 = await stream.recv()
        assert out2 == b"line2"
        # EOF is decided by is_open(), NOT by an empty read — an open-but-drained
        # stream blocks for the next frame (live-verified 2026-06-25: real
        # WSClient.read_stdout returns "" for both "no stdout this frame" and
        # "closed"). So close the fake to get the b"" EOF.
        stream._fake_ws._open_after_drain = False  # type: ignore[attr-defined]
        assert await stream.recv() == b""

    async def test_sendall_before_connect_raises(self):
        s = KimoExecStream()
        with pytest.raises(RuntimeError):
            await s.sendall(b"x")

    async def test_close_marks_ws_closed(
        self, stream: KimoExecStream, monkeypatch: pytest.MonkeyPatch
    ):
        tp = TokenProvider("ak", "sk", "cn-x")

        async def fake_fetch():
            return "tok"

        monkeypatch.setattr(tp, "_fetch_token", fake_fetch)
        await stream.connect("ep", "ns", "pod", tp)
        await stream.close()
        assert stream._fake_ws.closed is True  # type: ignore[attr-defined]
        assert stream.at_eof() is True


async def _connect(stream: KimoExecStream, ws: _FakeWS, monkeypatch: pytest.MonkeyPatch):
    tp = TokenProvider("ak", "sk", "cn-x")

    async def fake_fetch():
        return "tok"

    monkeypatch.setattr(tp, "_fetch_token", fake_fetch)
    monkeypatch.setattr(stream, "_open_ws", lambda url, token: ws)
    await stream.connect("ep", "ns", "pod", tp)


class TestReadline:
    async def test_readline_reassembles_one_line_per_call(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # stdout arrives as arbitrary chunks split across line boundaries.
        ws = _FakeWS(stdout_queue=['{"a":1}\n{"b":', "2}\n", '{"c":3}\n'])
        s = KimoExecStream()
        await _connect(s, ws, monkeypatch)
        assert await s.readline() == b'{"a":1}\n'
        assert await s.readline() == b'{"b":2}\n'
        assert await s.readline() == b'{"c":3}\n'

    async def test_readline_flushes_trailing_partial_then_eof(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # No trailing newline, then stream closes.
        ws = _FakeWS(stdout_queue=['{"partial":true}'], open_after_drain=False)
        s = KimoExecStream()
        await _connect(s, ws, monkeypatch)
        assert await s.readline() == b'{"partial":true}'  # trailing partial flushed once
        assert await s.readline() == b""  # then EOF
        assert s.at_eof() is True

    async def test_readline_eof_when_closed_with_empty_buffer(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        ws = _FakeWS(stdout_queue=[], open_after_drain=False)
        s = KimoExecStream()
        await _connect(s, ws, monkeypatch)
        assert await s.readline() == b""
        assert s.at_eof() is True


class TestErrorAndReturncode:
    async def test_returncode_reads_ws_attr(self, monkeypatch: pytest.MonkeyPatch):
        ws = _FakeWS(stdout_queue=[])
        ws.returncode = 137
        s = KimoExecStream()
        await _connect(s, ws, monkeypatch)
        assert s.returncode() == 137

    async def test_read_error_drains_error_channel(self, monkeypatch: pytest.MonkeyPatch):
        ws = _FakeWS(stdout_queue=[])
        ws.error_queue = ['{"status":"Failure","reason":"NonZeroExitCode"}']
        s = KimoExecStream()
        await _connect(s, ws, monkeypatch)
        err = s.read_error()
        assert b"NonZeroExitCode" in err

    def test_returncode_none_before_connect(self):
        assert KimoExecStream().returncode() is None
