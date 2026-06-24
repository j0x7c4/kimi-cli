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

# Worker command launched inside the sandbox via exec. The sandbox image's
# entrypoint normally runs ``/start-sandbox.sh``; for warm-pool BIND/attach we
# exec the worker bootstrap directly (spec §5 stdin:true + 上游 §3.4 BIND).
DEFAULT_EXEC_COMMAND = ["/start-sandbox.sh"]


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

        Uses ``kubernetes.stream.ws_client.WSClient`` for proven channel framing.
        Imported lazily so the docker path needs neither ``kubernetes`` nor
        ``websocket-client``.

        M0 NOTE (spec §9-8, ★最大风险点): ``WSClient`` derives TLS / proxy options
        from a ``kubernetes.client.Configuration``. Since CCI exec auth is the
        ``X-Auth-Token`` header (not a kubeconfig), we build a bare Configuration
        with ``host`` set and verify_ssl on. The end-to-end handshake (101 +
        channel frames) MUST be validated against a real Pod during M0 — it can't
        be exercised by curl/postman (官方明示). Tests stub this method.
        """
        from kubernetes.client import Configuration  # noqa: PLC0415
        from kubernetes.stream.ws_client import WSClient  # noqa: PLC0415

        configuration = Configuration()
        configuration.host = url
        configuration.verify_ssl = True
        headers = [f"X-Auth-Token: {token}"]
        return WSClient(
            configuration=configuration,
            url=url,
            headers=headers,
            capture_all=True,
        )

    async def sendall(self, data: bytes) -> None:
        """Write ``data`` to the sandbox worker's stdin (channel 0)."""
        if self._ws is None:
            raise RuntimeError("KimoExecStream.sendall before connect")
        # WSClient.write_stdin handles the channel-0 prefix framing.
        self._ws.write_stdin(data.decode("utf-8") if isinstance(data, (bytes, bytearray)) else data)

    async def recv(self, n: int = -1) -> bytes:
        """Read one demuxed stdout (channel 1) chunk, like docker attach_socket().

        Non-stdout channels are siphoned into internal buffers (stderr/error)
        rather than returned, so the caller's JSON-RPC framing on stdout is
        never corrupted. Returns ``b""`` when the stream is closed (EOF).
        """
        chunk = await self._recv_stdout_chunk()
        return chunk if chunk is not None else b""

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
            if chunk is None or chunk == b"":
                # Stream closed: flush any trailing partial line, then signal EOF.
                self._eof = True
                if self._stdout_buf:
                    line = bytes(self._stdout_buf)
                    self._stdout_buf.clear()
                    return line
                return b""
            self._stdout_buf.extend(chunk)
        idx = self._stdout_buf.index(b"\n")
        line = bytes(self._stdout_buf[: idx + 1])
        del self._stdout_buf[: idx + 1]
        return line

    async def _recv_stdout_chunk(self) -> bytes | None:
        """Read one channel-1 chunk; ``None`` when the WebSocket is closed (EOF)."""
        if self._ws is None:
            raise RuntimeError("KimoExecStream.recv before connect")
        out = self._ws.read_stdout(timeout=None)
        if out is None:
            # WSClient returns None both for "nothing yet" and "closed"; treat a
            # closed socket as EOF so the read loop can exit deterministically.
            if not self._is_ws_open():
                return None
            return b""
        return out.encode("utf-8") if isinstance(out, str) else bytes(out)

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
        """Exec command exit code (parsed from the error channel by WSClient)."""
        if self._ws is None:
            return None
        return getattr(self._ws, "returncode", None)

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
