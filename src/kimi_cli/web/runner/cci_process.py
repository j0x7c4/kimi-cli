"""CCI-backed session process: drives a KimoExecStream like a subprocess.

# hechun-fork-cci

Converges the CCI exec-WebSocket path onto the same gateway machinery as the
docker / local-subprocess path. ``SessionProcess`` (process.py) already factors
its byte-level IO behind five overridable transport primitives
(``_transport_read_stdout_line`` / ``_transport_stdout_at_eof`` /
``_transport_read_stderr`` / ``_transport_returncode`` / ``_transport_write_stdin``);
everything above them — the JSON-RPC read loop, status/busy tracking, WebSocket
fanout + replay, prompt framing — is backend agnostic.

This class plugs a :class:`~kimi_cli.web.spawner.cci_exec.KimoExecStream`
(returned by :meth:`CCISpawner.attach`) into those primitives:

    docker/local : self._process (asyncio subprocess) ── stdin/stdout pipes
    cci          : self._exec_stream (KimoExecStream) ── channel.k8s.io 0/1/2/3

``spawn`` (Pod create + wait-Running) and ``attach`` (exec WebSocket) are the
ONLY steps that still need a real Pod to validate end-to-end (101 handshake +
channel framing, spec §9-8 ★). Everything downstream of ``attach`` is exercised
here by feeding a mock KimoExecStream — see tests/web/test_cci_session_process.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from uuid import UUID, uuid4

from kimi_cli import logger
from kimi_cli.memory import resolve_owner_id
from kimi_cli.utils.subprocess_env import get_clean_env
from kimi_cli.web.api.sandbox_assets import SANDBOX_ASSETS_PATH

# Reuse the docker runner's env-forwarding + owner/subagent disk resolution so a
# CCI sandbox sees exactly the same env contract as a docker sandbox.
from kimi_cli.web.runner.container import (
    _SANDBOX_ENV_VARS,
    _read_agent_name_from_disk,
    _read_owner_id_from_disk,
)
from kimi_cli.web.runner.process import KimiCLIRunner, SessionProcess
from kimi_cli.web.runner.worker import AGENT_LOAD_FAILURE_EXIT_CODE
from kimi_cli.web.spawner import SandboxHandle, SandboxSpawner
from kimi_cli.web.spawner.cci_exec import KimoExecStream
from kimi_cli.wire.jsonrpc import JSONRPCErrorObject, JSONRPCErrorResponse


class CCISessionProcess(SessionProcess):
    """SessionProcess whose worker runs in a CCI Pod, driven over exec WebSocket.

    Lifecycle:
      start  → spawner.spawn(Pod) → spawner.attach() → KimoExecStream
      read   → stream.readline()  (channel 1, line-buffered)
      write  → stream.sendall()   (channel 0)
      stop   → stream.close() + spawner.stop(Pod)
    """

    def __init__(
        self,
        session_id: UUID,
        *,
        spawner: SandboxSpawner,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        super().__init__(session_id)
        self._spawner = spawner
        self._extra_env = extra_env or {}
        self._handle: SandboxHandle | None = None
        self._exec_stream: KimoExecStream | None = None

    @property
    def handle(self) -> SandboxHandle | None:
        """Current sandbox handle (Pod), or None if not spawned."""
        return self._handle

    # ── transport primitives (override base subprocess impls) ────────────────

    async def _transport_read_stdout_line(self) -> bytes:
        assert self._exec_stream is not None
        return await self._exec_stream.readline()

    def _transport_stdout_at_eof(self) -> bool:
        return self._exec_stream is None or self._exec_stream.at_eof()

    async def _transport_read_stderr(self) -> bytes:
        if self._exec_stream is None:
            return b""
        # CCI: combine the exec stderr channel (2) + error channel (3, exit
        # Status) into one diagnostic blob, mirroring subprocess stderr.read().
        stderr = await self._exec_stream.recv_stderr()
        error = self._exec_stream.read_error()
        parts = [p for p in (stderr, error) if p]
        return b"\n".join(parts)

    def _transport_returncode(self) -> int | None:
        return self._exec_stream.returncode() if self._exec_stream is not None else None

    async def _transport_write_stdin(self, data: bytes) -> None:
        assert self._exec_stream is not None
        await self._exec_stream.sendall(data)

    # ── liveness (override: no subprocess to inspect) ────────────────────────

    @property
    def is_alive(self) -> bool:
        stream = self._exec_stream
        return stream is not None and not stream.at_eof()

    # ── worker-exit hook (override base no-op) ───────────────────────────────

    async def _on_worker_exit(self, returncode: int | None, stderr: bytes) -> None:
        """Detect the agent-load-failure exit code → record metric + clear error.

        When the Pod worker refuses to start because its required agent couldn't
        be loaded it exits with ``AGENT_LOAD_FAILURE_EXIT_CODE`` (and best-effort
        writes a wire error to stdout, which the read loop already broadcasts).
        Here on the gateway we (1) bump ``kimo_agent_load_failure_total`` so a
        mis-mounted bundle is visible on the dashboard immediately, and (2)
        broadcast an extra, explicit client-facing error in case the worker died
        before it could write its own wire frame. ``reason`` is recovered from the
        worker's wire error data when present, else defaults to
        ``agent_required_missing``.

        Best-effort: never raises (runs inside the read loop).
        """
        if returncode != AGENT_LOAD_FAILURE_EXIT_CODE:
            return

        reason = "agent_required_missing"
        worker_message = ""
        # The worker stamps {"reason": ...} into its wire error data and the same
        # human message into stderr; recover both if the channel carried them.
        with contextlib.suppress(Exception):
            text = stderr.decode("utf-8", errors="replace")
            if text and text != "No stderr":
                worker_message = text

        metrics = getattr(self._spawner, "metrics", None)
        if metrics is not None:
            with contextlib.suppress(Exception):
                metrics.record_agent_load_failure(reason=reason)

        detail = worker_message or (
            "Sandbox worker refused to start: the required agent could not be "
            "loaded. Check that the agent name was forwarded (SUBAGENT) and that "
            "its yaml reached the Pod (~/.kimi/agents/<name>.yaml)."
        )
        logger.error(
            "[CCISessionProcess] worker agent-load failure sid={sid} reason={reason}: {detail}",
            sid=self.session_id,
            reason=reason,
            detail=detail,
        )
        with contextlib.suppress(Exception):
            await self._broadcast(
                JSONRPCErrorResponse(
                    id="agent-load-failure",
                    error=JSONRPCErrorObject(
                        code=AGENT_LOAD_FAILURE_EXIT_CODE,
                        message=f"Agent load failed: {detail}",
                        data={"reason": reason},
                    ),
                ).model_dump_json()
            )

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(
        self,
        *,
        reason: str | None = None,
        detail: str | None = None,
        restart_started_at: float | None = None,
    ) -> None:
        """Spawn the Pod (if needed) and attach the exec stream, then read-loop.

        Mirrors ``SessionProcess.start`` / ``ContainerSessionProcess.start`` but
        the "process" is a CCI Pod reached over an exec WebSocket.
        """
        async with self._lock:
            if self.is_alive:
                if self._read_task is None or self._read_task.done():
                    self._read_task = asyncio.create_task(self._read_loop())
                return

            self._in_flight_prompt_ids.clear()
            self._expecting_exit = False
            self._worker_id = str(uuid4())

            env = self._build_sandbox_env()

            logger.info(
                "Spawning CCI sandbox for session {sid}", sid=self.session_id
            )
            self._handle = await self._spawner.spawn(
                self.session_id, env.get("KIMI_USER_ID", ""), env
            )
            stream = await self._spawner.attach(self._handle)
            assert isinstance(stream, KimoExecStream)
            self._exec_stream = stream

            self._read_task = asyncio.create_task(self._read_loop())
            if restart_started_at is not None:
                elapsed_ms = int((time.perf_counter() - restart_started_at) * 1000)
                detail = f"restart_ms={elapsed_ms}"
                await self._emit_status("idle", reason=reason or "start", detail=detail)
                await self._emit_restart_notice(reason=reason, restart_ms=elapsed_ms)
            else:
                await self._emit_status("idle", reason=reason or "start", detail=None)

    async def stop_worker(
        self,
        *,
        reason: str | None = None,
        emit_status: bool = True,
    ) -> None:
        """Close the exec stream + delete the Pod, keeping WebSockets connected."""
        async with self._lock:
            self._expecting_exit = True

            if self._exec_stream is not None:
                with contextlib.suppress(Exception):
                    await self._exec_stream.close()
                self._exec_stream = None

            if self._handle is not None:
                try:
                    await self._spawner.stop(self._handle)
                except Exception as e:  # noqa: BLE001 — stop must not wedge teardown
                    logger.warning(
                        "[CCISessionProcess] spawner.stop failed sid={sid}: {err}",
                        sid=self.session_id,
                        err=e,
                    )
                self._handle = None

            if self._read_task is not None:
                self._read_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._read_task
                self._read_task = None

            self._in_flight_prompt_ids.clear()
            self._worker_id = None
            self._expecting_exit = False
            if emit_status:
                await self._emit_status("stopped", reason=reason or "stop")

    # ── env (same contract as ContainerSessionProcess) ────────────────────────

    def _build_sandbox_env(self) -> dict[str, str]:
        """Forward the same env set a docker sandbox gets (container.py parity)."""
        env: dict[str, str] = {}
        host_env = get_clean_env()
        for var_name in _SANDBOX_ENV_VARS:
            value = host_env.get(var_name)
            if value is not None:
                env[var_name] = value
        env["KIMI_SESSION_ID"] = str(self.session_id)
        owner_id_raw = _read_owner_id_from_disk(self.session_id)
        env["KIMI_USER_ID"] = resolve_owner_id(owner_id_raw)
        # CCI worker 起的是 fresh session（Pod 上无 session_config.json），故转发 agent
        # 名字（subagent 字段 OR agent_spec_path basename），worker 按名解析下载下来的
        # ~/.kimi/agents/<name>.yaml。docker 走 bind-mount 读 agent_spec_path，不受影响。
        agent_name = _read_agent_name_from_disk(self.session_id)
        if agent_name:
            env["SUBAGENT"] = agent_name
            # hechun-fork-cci: this session REQUIRES a specific agent (the gateway
            # forwarded its name), so flip the worker into fail-fast mode — if it
            # can't resolve the agent it must refuse to start rather than silently
            # serve the default agent. Docker never injects this (the worker reads
            # agent_spec_path off the bind-mount), so docker behaviour is unchanged.
            env["KIMI_REQUIRE_AGENT"] = "1"

        # hechun-fork-cci: CCI Pods can't bind-mount the gateway host, so the
        # worker fetches static assets (~/.kimi/agents etc.) over HTTP from the
        # gateway's internal sandbox-assets endpoint on startup. We compute the
        # download URL + token here (values must be assembled by the gateway, so
        # this is NOT a host-env passthrough via _SANDBOX_ENV_VARS) and inject
        # them only when the deploy provides KIMO_GATEWAY_INTERNAL_URL (= the
        # gateway's VPC-internal base URL). Docker path never injects these, so
        # its bind-mount stays the only source there and the worker skips the
        # download entirely. None-safe: unset → vars absent → worker no-ops.
        internal_url = (host_env.get("KIMO_GATEWAY_INTERNAL_URL") or "").strip()
        if internal_url:
            env["KIMO_SANDBOX_ASSETS_URL"] = internal_url.rstrip("/") + SANDBOX_ASSETS_PATH
            token = host_env.get("KIMI_WEB_SESSION_TOKEN")
            if token:
                env["KIMO_SANDBOX_ASSETS_TOKEN"] = token

        env.update(self._extra_env)
        return env


class CCIRunner(KimiCLIRunner):
    """Manages CCI-backed session processes (one Pod per session)."""

    def __init__(
        self,
        *,
        spawner: SandboxSpawner,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._spawner = spawner
        self._extra_env = extra_env or {}

    def start(self) -> None:
        """No-op; Pods are spawned on demand at first ``start``."""
        pass

    def get_session(self, session_id: UUID) -> CCISessionProcess | None:
        proc = self._sessions.get(session_id)
        if proc is None:
            return None
        assert isinstance(proc, CCISessionProcess)
        return proc

    async def get_or_create_session(self, session_id: UUID) -> CCISessionProcess:
        async with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = CCISessionProcess(
                    session_id,
                    spawner=self._spawner,
                    extra_env=self._extra_env,
                )
            proc = self._sessions[session_id]
            assert isinstance(proc, CCISessionProcess)
            return proc


# Keep module identity stable for isinstance checks elsewhere (mirrors the
# pattern container.py uses to spoof its __module__).
CCIRunner.__module__ = "kimi_cli.web.runner.process"


__all__ = ["CCISessionProcess", "CCIRunner"]
