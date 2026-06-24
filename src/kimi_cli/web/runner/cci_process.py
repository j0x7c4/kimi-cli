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

# Reuse the docker runner's env-forwarding + owner/subagent disk resolution so a
# CCI sandbox sees exactly the same env contract as a docker sandbox.
from kimi_cli.web.runner.container import (
    _SANDBOX_ENV_VARS,
    _read_owner_id_from_disk,
    _read_subagent_from_disk,
)
from kimi_cli.web.runner.process import KimiCLIRunner, SessionProcess
from kimi_cli.web.spawner import SandboxHandle, SandboxSpawner
from kimi_cli.web.spawner.cci_exec import KimoExecStream


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
        subagent = _read_subagent_from_disk(self.session_id)
        if subagent:
            env["SUBAGENT"] = subagent
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
