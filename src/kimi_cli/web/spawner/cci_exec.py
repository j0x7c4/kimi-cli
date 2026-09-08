"""CCI Pod exec WebSocket adapter (spec §3/§6).

# hechun-fork-cci

The CCI exec endpoint is a Kubernetes-style streaming exec:

    GET wss://{endpoint}/apis/cci/v2/namespaces/{ns}/pods/{pod}/exec
        ?command=...&stdin=true&stdout=true&stderr=true&tty=false
    header: X-Auth-Token: <iam token>   (spec §1.2: token only for exec handshake)
    subprotocol: channel.k8s.io
    GET → 101 Switching Protocols

Over the upgraded socket every frame is prefixed with one channel byte
(``channel.k8s.io`` framing):

    0 = stdin   1 = stdout   2 = stderr   3 = error   4 = resize

:class:`KimoExecStream` reuses ``kubernetes.stream.ws_client`` to do the channel
(de)multiplexing and presents the SAME duck-typed API the upstream
``WSStreamProxy`` already drives for a docker ``container.attach_socket()`` —
namely ``sendall(bytes)`` (writes to stdin / channel 0) and ``recv(n) -> bytes``
(reads demuxed stdout, channel 1). This keeps the gateway proxy layer zero-touch
(spec §3 "对上层 WSStreamProxy 暴露与原 docker attach_socket() 一致的 API").
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kimi_cli.web.spawner.cci_auth import TokenProvider

# channel.k8s.io stream channels.
STDIN_CHANNEL = 0
STDOUT_CHANNEL = 1
STDERR_CHANNEL = 2
ERROR_CHANNEL = 3
RESIZE_CHANNEL = 4

WS_SUBPROTOCOL = "channel.k8s.io"

# Subprotocol CCI 2.0 actually negotiates for exec (live-verified 2026-06-25:
# bare "channel.k8s.io" and v5 both fail; v4 → 101). channel byte framing is the
# same v4 protocol kubernetes WSClient demuxes.
EXEC_SUBPROTOCOL = "v4.channel.k8s.io"

# WS handshake timeout (s); cleared to None after connect for streaming reads.
_HANDSHAKE_TIMEOUT_S = 20

# Worker command launched inside the sandbox via exec. The sandbox image's
# entrypoint normally runs ``/start-sandbox.sh``; for warm-pool BIND/attach we
# exec the worker bootstrap directly (spec §5 stdin:true + 上游 §3.4 BIND).
DEFAULT_EXEC_COMMAND = ["/start-sandbox.sh"]

# Warm-pool exec command: the two-phase worker entry. ``/start-sandbox.sh`` execs
# ``worker "$KIMI_SESSION_ID"``, which needs a session id the warm Pod does not
# have yet; ``--warm`` prepares everything session-independent and then blocks on
# the warm handshake (web/runner/warm_protocol.py) until a bind frame claims it.
WARM_EXEC_COMMAND = ["python", "-m", "kimi_cli.web.runner.worker", "--warm"]


def build_exec_url(endpoint: str, ns: str, pod: str, command: list[str]) -> str:
    """Build the wss exec URL with channel query params (spec §3).

    ``tty=false`` so stdout / stderr stay on distinct channels (1 / 2) — required
    for the JSON-RPC protocol the worker speaks over stdout.
    """
    from urllib.parse import quote, urlencode  # noqa: PLC0415

    base = f"wss://{endpoint}/apis/cci/v2/namespaces/{quote(ns)}/pods/{quote(pod)}/exec"
    # k8s exec wants one ``command`` query param per argv element.
    params = [("command", c) for c in command]
    params += [
        ("stdin", "true"),
        ("stdout", "true"),
        ("stderr", "true"),
        ("tty", "false"),
    ]
    return f"{base}?{urlencode(params)}"


class KimoExecStream:
    """docker ``attach_socket()``-equivalent over CCI exec WebSocket.

    Lifecycle: ``await connect(...)`` then ``sendall`` / ``recv`` / ``close``.

    Channel demux: stdin writes are prefixed channel 0; reads return only
    channel-1 (stdout) payload, mirroring the byte stream a docker
    ``attach_socket()`` yields after demuxing the docker stream header. stderr
    (channel 2) and error (channel 3) frames are surfaced via ``recv_stderr`` /
    are raised on close so the gateway can log them without polluting the
    JSON-RPC stdout stream.
    """

    def __init__(self) -> None:
        self._ws: object | None = None  # kubernetes.stream.ws_client.WSClient
        self._stderr_buf = bytearray()
        self._error_buf = bytearray()
        # stdout line buffer: read_stdout() returns arbitrary chunks, but the
        # worker speaks newline-delimited JSON-RPC. ``readline`` reassembles
        # exactly one line per call (docker attach_socket()/StreamReader parity).
        self._stdout_buf = bytearray()
        self._eof = False

    async def connect(
        self,
        endpoint: str,
        ns: str,
        pod: str,
        token_provider: TokenProvider,
        *,
        command: list[str] | None = None,
    ) -> None:
        """Open the exec WebSocket and complete the 101 handshake.

        The IAM token is fetched lazily from ``token_provider`` (cached 22h,
        spec §2). The handshake validates the token once; the stream then stays
        up for the session lifetime without re-checking it (spec §1.2).
        """
        token = await token_provider.token()
        url = build_exec_url(endpoint, ns, pod, command or DEFAULT_EXEC_COMMAND)
        self._ws = self._open_ws(url, token)

    def _open_ws(self, url: str, token: str) -> object:
        """Open the channel.k8s.io WebSocket (overridable in tests).

        Reuses ``kubernetes.stream.ws_client.WSClient`` for its proven channel
        (de)muxing, but builds the underlying socket ourselves — the kubernetes
        helper ``create_websocket`` only forwards an ``authorization`` header and
        silently drops everything else, so the CCI ``X-Auth-Token`` never reaches
        the server and the handshake 401s behind CloudWAF.

        M0 LIVE-VERIFIED recipe (spec §9-8, ★最大风险点 — 真机钉死, 2026-06-25):
        - header ``X-Auth-Token: <iam token>`` passed straight to websocket-client
          (NOT through ``kubernetes...create_websocket``).
        - subprotocol ``v4.channel.k8s.io`` (CCI negotiates v4, not bare
          ``channel.k8s.io`` nor v5). → ``101 Switching Protocols``.
        The socket is then wrapped in a ``WSClient`` shell (same attrs its
        ``__init__`` sets, minus the broken ``create_websocket`` call) so all the
        downstream channel demux (read_stdout/read_stderr/read_channel/...) is
        unchanged. Tests stub this whole method.
        """
        import ssl  # noqa: PLC0415
        from io import StringIO  # noqa: PLC0415

        import certifi  # noqa: PLC0415
        import websocket  # noqa: PLC0415 — websocket-client
        from kubernetes.stream.ws_client import WSClient  # noqa: PLC0415

        sslopt = {"cert_reqs": ssl.CERT_REQUIRED, "ca_certs": certifi.where()}
        sock = websocket.create_connection(
            url,
            header=[f"X-Auth-Token: {token}"],
            subprotocols=[EXEC_SUBPROTOCOL],
            sslopt=sslopt,
            skip_utf8_validation=True,
            timeout=_HANDSHAKE_TIMEOUT_S,
        )
        # Streaming reads must block (WSClient.update() gates readability via
        # select); clear the handshake timeout so idle exec streams don't trip
        # WebSocketTimeoutException.
        sock.settimeout(None)

        # Build a WSClient around the pre-connected socket — replicate the bits
        # WSClient.__init__(capture_all=True) sets, skipping its create_websocket.
        client = WSClient.__new__(WSClient)
        client._connected = False
        client._channels = {}
        client._closed_channels = set()
        client.subprotocol = getattr(sock, "subprotocol", None)
        client.binary = False
        client.newline = "\n"
        client._all = StringIO()
        client.sock = sock
        client._connected = True
        client._returncode = None
        return client

    async def sendall(self, data: bytes) -> None:
        """Write ``data`` to the sandbox worker's stdin (channel 0)."""
        if self._ws is None:
            raise RuntimeError("KimoExecStream.sendall before connect")
        # WSClient.write_stdin handles the channel-0 prefix framing.
        self._ws.write_stdin(data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data)

    async def keepalive(self) -> None:
        """Send one zero-length stdin frame purely to keep the exec stream alive.

        🔴 Why this exists (2026-09-08, four samples on prod + test): the CCI exec
        WebSocket is closed by the far side after **exactly 5 minutes** of no
        traffic. It is an *idle* timer, not a connection age limit — prod
        2026-09-06 died 5m00s after its last frame, not 5m00s after connecting.
        We never noticed because nothing on our side pings: ``create_connection``
        is followed by ``settimeout(None)`` and there is no ping/pong anywhere in
        this file. So a user who pauses for five minutes comes back to a dead
        stream: their next message lands in a closed socket, produces no turn at
        all (and an unfinished turn is never persisted, so it vanishes), and the
        session ends up reclaimed in ``error``.

        Why a zero-length channel-0 frame rather than a WebSocket PING:
        ``write_channel`` sends ``chr(0) + data``, so an empty payload still puts
        a real frame on the wire, and the API server forwards **zero bytes** to
        the worker's stdin — invisible to the JSON-RPC framing, no protocol
        change, nothing for the worker to parse. A WS-level PING would be even
        cheaper but only resets timers that live at the WS/LB layer; if the far
        side counts *exec channel* traffic instead, a PING would not help and the
        stream would still die. This frame satisfies both readings.

        Safe to call while the read loop is blocked: reads run in a worker thread
        (``asyncio.to_thread``) and ``send_frame`` takes the socket's own lock —
        the same concurrency the existing ``sendall`` has always relied on.
        """
        if self._ws is None:
            raise RuntimeError("KimoExecStream.keepalive before connect")
        self._ws.write_stdin("")

    async def recv(self, n: int = -1) -> bytes:
        """Read one demuxed stdout (channel 1) chunk, like docker attach_socket().

        Non-stdout channels are siphoned into internal buffers (stderr/error)
        rather than returned, so the caller's JSON-RPC framing on stdout is
        never corrupted. Blocks until stdout data arrives; returns ``b""`` only
        when the stream is closed (EOF).
        """
        while True:
            chunk = await self._recv_stdout_chunk()
            if chunk is None:
                return b""  # real EOF
            if chunk:
                return chunk
            # transient: the frame just read carried no stdout (stderr/error/
            # keepalive) — keep polling rather than mistaking it for EOF.

    async def readline(self) -> bytes:
        """Return exactly one newline-terminated stdout line (StreamReader parity).

        Buffers chunks from :meth:`recv` until a ``\\n`` is seen, mirroring
        ``asyncio.StreamReader.readline`` so the gateway read loop (which expects
        one JSON-RPC frame per ``readline``) is backend-agnostic. On EOF returns
        any trailing partial line once, then ``b""`` forever (sets ``at_eof``).
        """
        if self._ws is None:
            raise RuntimeError("KimoExecStream.readline before connect")
        while b"\n" not in self._stdout_buf:
            chunk = await self._recv_stdout_chunk()
            if chunk is None:
                # Stream closed: flush any trailing partial line, then signal EOF.
                self._eof = True
                if self._stdout_buf:
                    line = bytes(self._stdout_buf)
                    self._stdout_buf.clear()
                    return line
                return b""
            if not chunk:
                # Transient empty: the frame just read was on a non-stdout
                # channel; keep polling (update() blocked one frame, no spin).
                continue
            self._stdout_buf.extend(chunk)
        idx = self._stdout_buf.index(b"\n")
        line = bytes(self._stdout_buf[: idx + 1])
        del self._stdout_buf[: idx + 1]
        return line

    async def _recv_stdout_chunk(self) -> bytes | None:
        """Read one channel-1 chunk (tri-state, live-verified 2026-06-25).

        - ``bytes`` (non-empty) — stdout payload.
        - ``b""``             — the frame just read carried no stdout (it was on
                                the stderr/error channel, or empty); the socket
                                is still open, so the caller must keep polling.
        - ``None``            — EOF: the socket is closed.

        ``WSClient.read_stdout(timeout=None)`` (binary=False) returns ``""`` for
        BOTH "no stdout this frame" and "closed" — so emptiness alone is NOT EOF.
        EOF is decided solely by ``is_open()``. Conflating the two made every
        exec read return zero output (the first frame races ahead of the
        command's stdout). ``update(timeout=None)`` blocks one frame per call, so
        re-polling on ``b""`` is not a busy-spin.
        """
        if self._ws is None:
            raise RuntimeError("KimoExecStream.recv before connect")
        # ★ 关键：WSClient.read_stdout(timeout=None) 是同步阻塞调用（poll.poll(None)）。
        # 直接在 async 读循环里调会**冻结整个 asyncio 事件循环** —— worker idle 时永久阻塞，
        # gateway 收不到 client prompt、干不了任何事（2026-06-25 整链实测：事件循环卡死）。
        # offload 到线程：阻塞发生在线程里，事件循环空出来处理 receive loop / 写 stdin 等。
        out = await asyncio.to_thread(self._ws.read_stdout, None)
        if out:
            return out.encode("utf-8") if isinstance(out, str) else bytes(out)
        if not self._is_ws_open():
            return None
        return b""

    def at_eof(self) -> bool:
        """Whether the stdout stream has reached EOF (worker exited)."""
        return self._eof or not self._is_ws_open()

    async def recv_stderr(self) -> bytes:
        if self._ws is None:
            raise RuntimeError("KimoExecStream.recv_stderr before connect")
        err = self._ws.read_stderr(timeout=0)
        if err:
            chunk = err.encode("utf-8") if isinstance(err, str) else bytes(err)
            self._stderr_buf.extend(chunk)
        return bytes(self._stderr_buf)

    def read_error(self) -> bytes:
        """Drain the exec error channel (3): exit status JSON / k8s Status object.

        This is the CCI analog of subprocess stderr — the channel.k8s.io error
        channel carries the command's failure ``Status`` (non-zero exit, signal).
        """
        if self._ws is None:
            return bytes(self._error_buf)
        try:
            err = self._ws.read_channel(ERROR_CHANNEL, timeout=0)
        except Exception:  # noqa: BLE001 — best-effort diagnostic
            err = None
        if err:
            chunk = err.encode("utf-8") if isinstance(err, str) else bytes(err)
            self._error_buf.extend(chunk)
        return bytes(self._error_buf)

    def returncode(self) -> int | None:
        """Exec command exit code (parsed from the error channel by WSClient).

        ``WSClient.returncode`` is a property that, on a closed stream, does
        ``yaml.safe_load(read_channel(ERROR))['status']`` — if the error channel
        is empty (no exit Status frame) ``safe_load("")`` is ``None`` → ``None[
        'status']`` raises ``'NoneType' object is not subscriptable`` (live-
        verified 2026-06-25: this crashed the gateway read loop on worker exit,
        masking the real stderr). Guard it: unknown exit code → ``None``.
        """
        if self._ws is None:
            return None
        try:
            return self._ws.returncode
        except Exception:  # noqa: BLE001 — WSClient.returncode 可能因空 error 通道崩
            return None

    def _is_ws_open(self) -> bool:
        if self._ws is None:
            return False
        is_open = getattr(self._ws, "is_open", None)
        if callable(is_open):
            try:
                return bool(is_open())
            except Exception:  # noqa: BLE001
                return False
        return True

    async def close(self) -> None:
        self._eof = True
        if self._ws is not None:
            try:
                self._ws.close()
            finally:
                self._ws = None


def demux_frame(frame: bytes) -> tuple[int, bytes]:
    """Split a raw channel.k8s.io frame into ``(channel, payload)``.

    Pure helper (no IO) so the framing contract is unit-testable without a live
    WebSocket. The first byte is the channel id; the rest is payload.
    """
    if not frame:
        return (-1, b"")
    return (frame[0], frame[1:])


def frame_stdin(payload: bytes) -> bytes:
    """Prefix ``payload`` with the stdin channel byte (channel 0)."""
    return bytes([STDIN_CHANNEL]) + payload


__all__ = [
    "KimoExecStream",
    "build_exec_url",
    "demux_frame",
    "frame_stdin",
    "STDIN_CHANNEL",
    "STDOUT_CHANNEL",
    "STDERR_CHANNEL",
    "ERROR_CHANNEL",
    "RESIZE_CHANNEL",
    "WS_SUBPROTOCOL",
]
