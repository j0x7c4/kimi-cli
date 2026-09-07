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
from typing import Any
from uuid import UUID, uuid4

from kimi_cli import logger
from kimi_cli.memory import resolve_owner_id
from kimi_cli.utils.subprocess_env import get_clean_env
from kimi_cli.web.api.sandbox_assets import SANDBOX_ASSETS_PATH

# Reuse the docker runner's env-forwarding + owner/subagent disk resolution so a
# CCI sandbox sees exactly the same env contract as a docker sandbox.
from kimi_cli.web.runner.container import (
    _SANDBOX_ENV_VARS,
    _memory_via_gateway_flag,
    _read_agent_name_from_disk,
    _read_owner_id_from_disk,
)
from kimi_cli.web.runner.process import KimiCLIRunner, SessionProcess
from kimi_cli.web.runner.worker import AGENT_LOAD_FAILURE_EXIT_CODE
from kimi_cli.web.spawner import SandboxHandle, SandboxSpawner
from kimi_cli.web.spawner.cci_exec import KimoExecStream
from kimi_cli.wire.jsonrpc import JSONRPCErrorObject, JSONRPCErrorResponse


def build_warm_sandbox_env(agent_name: str) -> dict[str, str]:
    """Env for a warm Pod: everything a sandbox needs EXCEPT an identity.

    Same passthrough set as :meth:`CCISessionProcess._build_sandbox_env`, minus
    ``KIMI_SESSION_ID`` / ``KIMI_USER_ID`` — a warm Pod is created before any
    session exists, and those two arrive later in the bind frame. This is not a
    convenience: baking a placeholder identity into the Pod env would be read by
    the MCP config's ``${KIMI_USER_ID}`` substitution, and the resulting
    connection is never rebuilt after binding (spec §4.4).
    """
    env: dict[str, str] = {}
    host_env = get_clean_env()
    for var_name in _SANDBOX_ENV_VARS:
        value = host_env.get(var_name)
        if value is not None:
            env[var_name] = value
    if agent_name:
        env["SUBAGENT"] = agent_name
        env["KIMI_REQUIRE_AGENT"] = "1"
    internal_url = (host_env.get("KIMO_GATEWAY_INTERNAL_URL") or "").strip()
    if internal_url:
        env["KIMO_SANDBOX_ASSETS_URL"] = internal_url.rstrip("/") + SANDBOX_ASSETS_PATH
        token = host_env.get("KIMI_WEB_SESSION_TOKEN")
        if token:
            env["KIMO_SANDBOX_ASSETS_TOKEN"] = token
    via_gateway = _memory_via_gateway_flag()
    if via_gateway is not None:
        env["KIMO_MEMORY_VIA_GATEWAY"] = via_gateway
    return env


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_bind_identity(session_id: UUID, env: dict[str, str]) -> tuple[str, bool]:
    """Resolve the ``(owner_id, yolo)`` a warm-pool bind frame must carry.

    🔴 These two values cannot be left to the worker to discover. On CCI the
    worker's storage is a ``RemoteKimoStorage`` whose ``load_session_state`` is
    inert by design (always None), so a worker that "reads its own state" reads
    nothing and silently keeps ``yolo=False`` — which strands every tool call on
    an approval prompt the iOS/Flutter UIs cannot answer. The gateway, by
    contrast, wrote ``kimo_session_state`` itself and can read it back, so it
    resolves both here and hard-fails the claim if the result is unusable
    (spec §4.3).

    Blocking (SQLAlchemy) — call from a thread.
    """
    owner_id = env.get("KIMI_USER_ID", "")
    yolo = _truthy(env.get("KIMO_DEFAULT_YOLO"))
    try:
        from typing import cast

        from kimi_cli.session_state import load_session_state_via_storage
        from kimi_cli.storage import KimoStorage
        from kimi_cli.web.runner.process import _get_gateway_memory_storage

        # Same gateway-side storage the memory proxy uses (a MyKimoStorage that
        # CAN reach RDS, unlike the worker's inert RemoteKimoStorage).
        storage = cast("KimoStorage", _get_gateway_memory_storage())
        state = load_session_state_via_storage(session_id, storage)
        if state.owner_id:
            owner_id = state.owner_id
        yolo = yolo or bool(state.approval.yolo)
    except Exception as e:  # noqa: BLE001 — env values remain the fallback
        logger.warning(
            "[CCISessionProcess] could not read persisted state for sid={sid} "
            "({err}); binding with env-derived identity",
            sid=session_id,
            err=e,
        )
    return owner_id, yolo


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
        warm_pool: Any | None = None,
    ) -> None:
        super().__init__(session_id)
        self._spawner = spawner
        self._extra_env = extra_env or {}
        # Optional WarmPoolManager. When present, ``start`` tries to adopt an
        # already-warmed Pod before falling back to the cold spawn path; None
        # (docker path / pool disabled) keeps the original behaviour exactly.
        self._warm_pool = warm_pool
        # pod_name of the warm Pod this session adopted (None on the cold path).
        # Used to close its kimo_sandbox_pod row once the Pod is torn down.
        self._adopted_pod_name: str | None = None
        self._handle: SandboxHandle | None = None
        self._exec_stream: KimoExecStream | None = None
        # 冷启动分段计时（仅诊断日志，不影响任何行为）。start() 的冷启动路径填入
        # (T0 spawn 前, T1 spawn 返回=pod Running+IP, T2 attach 返回)；read-loop 收到
        # worker 首行 stdout 时补 T3 并打一行汇总。热复用（start 提前返回）不填、不打。
        self._cold_start_marks: tuple[float, float, float] | None = None
        self._cold_start_timing_pending: bool = False

    @property
    def handle(self) -> SandboxHandle | None:
        """Current sandbox handle (Pod), or None if not spawned."""
        return self._handle

    # ── transport primitives (override base subprocess impls) ────────────────

    async def _transport_read_stdout_line(self) -> bytes:
        assert self._exec_stream is not None
        line = await self._exec_stream.readline()
        # 冷启动整块加载耗时锚点：read-loop 首次拿到 worker 非空 stdout 行 = wire server
        # 起来 = 容器内 bundle/agent/MCP/知识库加载就绪。⚠️ 不能用 cci_process 在 attach 后
        # 立即 emit 的 idle 当就绪锚点（早于 worker 真就绪），必须用「首行 stdout」。
        if self._cold_start_timing_pending and line:
            self._log_cold_start_timing()
        return line

    def _log_cold_start_timing(self) -> None:
        """冷启动首就绪时打一行分段耗时汇总（gateway stdout 可 grep）。计时 exception-safe：
        任何异常都吞掉、绝不影响 read-loop。热复用不会走到这里（pending 仅冷启动置 True）。
        """
        # 先落 pending，确保即便日志抛异常也不会每行都重试。
        self._cold_start_timing_pending = False
        try:
            marks = self._cold_start_marks
            if marks is None:
                return
            t0, t1, t2 = marks
            t3 = time.perf_counter()
            logger.info(
                "[kimo][timing] sid={sid} cci_spawn={spawn}ms attach={attach}ms "
                "worker_load={load}ms total_to_ready={total}ms",
                sid=self.session_id,
                spawn=int((t1 - t0) * 1000),
                attach=int((t2 - t1) * 1000),
                load=int((t3 - t2) * 1000),
                total=int((t3 - t0) * 1000),
            )
        except Exception:  # noqa: BLE001 — 计时/日志绝不能弄崩 read-loop
            pass

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

    async def _release_pod_on_exit(self, *, reason: str) -> None:
        """Delete the Pod + close the exec stream after the worker has exited.

        hechun-fork-cci 主修复（worker 意外退出即删 Pod）. Called from inside the
        read loop (``_on_worker_exit``) once the worker is confirmed dead. A dead
        worker leaves the keepalive Pod (``sleep infinity`` 主进程 + 容器里常驻的
        Xvfb/Jupyter 等) Running forever → 永久计费泄漏. Before this, NO recovery
        path deleted a Pod when the worker died via ``process_exit`` /
        ``read_loop_error`` (session went ``error``) — outside the四类启动/sweeper/
        超时/撞名兜底. So reclaim it immediately.

        Crucially this does NOT call ``stop_worker()``: that cancels + awaits
        ``self._read_task``, but we ARE running inside that very task → it would
        cancel itself. Instead we delete the Pod directly via ``spawner.stop`` and
        drop the handle/stream in place; the read loop then finishes naturally and
        the periodic sweeper (web/app.py) is a further backstop if this is missed.

        Best-effort: never raises (runs inside the read loop).
        """
        # Mark the worker gone so is_alive() flips False and the sweeper / a later
        # stop_worker() won't try to re-delete or re-read a dead stream.
        self._expecting_exit = True

        handle = self._handle
        self._handle = None
        stream = self._exec_stream
        self._exec_stream = None

        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.close()

        await self._retire_adopted_row(reason="worker_exit")

        if handle is not None:
            try:
                await self._spawner.stop(handle)
                logger.info(
                    "[CCISessionProcess] worker exited → deleted Pod {pod} sid={sid} ({reason})",
                    pod=handle.handle_id,
                    sid=self.session_id,
                    reason=reason,
                )
            except Exception as e:  # noqa: BLE001 — delete failure must not wedge the loop
                logger.warning(
                    "[CCISessionProcess] worker-exit Pod delete failed sid={sid} pod={pod}: {err}",
                    sid=self.session_id,
                    pod=handle.handle_id,
                    err=e,
                )

    async def _retire_adopted_row(self, reason: str) -> None:
        """Close the ``kimo_sandbox_pod`` row of an adopted Pod after teardown."""
        pod_name = self._adopted_pod_name
        pool = self._warm_pool
        self._adopted_pod_name = None
        if pod_name is None or pool is None:
            return
        with contextlib.suppress(Exception):
            await pool.release(pod_name, reason)

    async def _on_worker_exit(self, returncode: int | None, stderr: bytes) -> None:
        """Worker died unexpectedly → delete its now-worthless Pod, then (for the
        agent-load-failure exit code) record the metric + broadcast a clear error.

        hechun-fork-cci. Two responsibilities:

        1. **【主修复 + _on_worker_exit 兜底】 always release the Pod.** A dead worker
           (any exit code, or a read-loop error) leaves the keepalive Pod Running
           forever — the leak口子 the原四类兜底 missed. We delete it here for EVERY
           exit code, not just ``AGENT_LOAD_FAILURE_EXIT_CODE`` (previously this hook
           special-cased that one code and no-op'd everything else →普通退出码 leaked).

        2. For ``AGENT_LOAD_FAILURE_EXIT_CODE`` specifically: bump
           ``kimo_agent_load_failure_total`` so a mis-mounted bundle is visible on the
           dashboard immediately, and broadcast an extra, explicit client-facing error
           in case the worker died before it could write its own wire frame. ``reason``
           is recovered from the worker's wire error data when present, else defaults
           to ``agent_required_missing``.

        Best-effort: never raises (runs inside the read loop).
        """
        # (1) 主修复：worker 死了 Pod 无价值 → 立即回收（所有退出码，含 read-loop error）。
        with contextlib.suppress(Exception):
            await self._release_pod_on_exit(reason=f"process_exit code={returncode}")

        # (2) Agent-load-failure 专属处理（仅该退出码）。
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

            # 冷启动分段计时锚点 T0（spawn 前）。perf_counter 不抛，纯诊断，不改任何行为。
            _timing_t0 = time.perf_counter()

            adopted = await self._adopt_warm_pod(env)
            if adopted is not None:
                # Warm path: the Pod already exists and its worker already
                # confirmed the bind, so "spawn" and "attach" both cost nothing
                # here. Timing marks are still filled in so the same
                # ``[kimo][timing]`` line can be compared against the cold baseline.
                self._handle, stream = adopted
                assert isinstance(stream, KimoExecStream)
                self._exec_stream = stream
                _timing_t1 = _timing_t2 = time.perf_counter()
            else:
                logger.info(
                    "Spawning CCI sandbox for session {sid}", sid=self.session_id
                )
                self._handle = await self._spawner.spawn(
                    self.session_id, env.get("KIMI_USER_ID", ""), env
                )
                _timing_t1 = time.perf_counter()  # spawn 返回：pod Running + podIP
                stream = await self._spawner.attach(self._handle)
                assert isinstance(stream, KimoExecStream)
                self._exec_stream = stream
                _timing_t2 = time.perf_counter()  # attach 返回：exec WS 握手完成
            # 交给 read-loop：收到 worker 首行 stdout 时补 T3 打汇总（_log_cold_start_timing）。
            self._cold_start_marks = (_timing_t0, _timing_t1, _timing_t2)
            self._cold_start_timing_pending = True

            self._read_task = asyncio.create_task(self._read_loop())
            # hechun-fork-cci: a fresh Pod worker was just spawned (this happens
            # every few minutes as CCI recycles the Pod / drops the exec stream).
            # Re-declare the client's capabilities to it FIRST — before any prompt —
            # so AskUserQuestion / plan-mode survive the restart. No-op until the
            # gateway has forwarded an initialize frame. This is THE fix for the
            # capability handshake not sticking across CCI worker respawns.
            await self._replay_initialize_to_worker()
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
            await self._retire_adopted_row(reason=reason or "stop")

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

    # ── warm pool adoption ────────────────────────────────────────────────────

    async def _adopt_warm_pod(
        self, env: dict[str, str]
    ) -> tuple[SandboxHandle, KimoExecStream] | None:
        """Try to take over an already-warmed Pod instead of spawning one.

        Returns the adopted (handle, stream) or ``None`` for "spawn normally".
        Every failure inside — pool empty, claim lost, bind rejected, pool
        unavailable — returns ``None``: the warm pool is an optimisation and must
        never be able to fail a session (spec §2).

        ⚠️ Ordering: :meth:`WarmPoolManager.acquire` has already written the bind
        frame and waited for the worker's confirmation by the time it returns.
        ``start`` then starts the read loop and only afterwards replays
        ``initialize`` — replaying it earlier would feed the initialize frame to a
        worker still blocked on its bind read, which would swallow it as the bind
        line (W0 spike finding #3).
        """
        pool = self._warm_pool
        if pool is None:
            return None
        try:
            owner_id, yolo = await asyncio.to_thread(
                _resolve_bind_identity, self.session_id, env
            )
            claimed = await pool.acquire(
                self.session_id,
                owner_id,
                yolo=yolo,
                agent=env.get("SUBAGENT"),
                env=env,
            )
        except Exception as e:  # noqa: BLE001 — never let the pool break a session
            logger.warning(
                "[CCISessionProcess] warm-pool acquire failed sid={sid}; cold start: {err}",
                sid=self.session_id,
                err=e,
            )
            return None
        if claimed is None:
            return None
        handle, stream = claimed
        self._adopted_pod_name = handle.handle_id
        logger.info(
            "[CCISessionProcess] adopted warm pod {pod} for session {sid}",
            pod=handle.handle_id,
            sid=self.session_id,
        )
        return handle, stream

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

        # hechun-fork-cci (方案 B): inject KIMO_MEMORY_VIA_GATEWAY (DB mode only) so
        # the worker delegates persistent memory to the gateway over the wire
        # instead of connecting to RDS from the Pod (which it cannot reach). This is
        # gateway-computed, NOT a host-env passthrough — the gateway's own env must
        # never carry it (else its build_storage() would also pick RemoteKimoStorage
        # and dead-end). None in file mode → not injected. CCI is always DB mode, so
        # in practice this is always "1" here; the guard keeps container/CCI parity.
        via_gateway = _memory_via_gateway_flag()
        if via_gateway is not None:
            env["KIMO_MEMORY_VIA_GATEWAY"] = via_gateway

        env.update(self._extra_env)
        return env


class CCIRunner(KimiCLIRunner):
    """Manages CCI-backed session processes (one Pod per session)."""

    def __init__(
        self,
        *,
        spawner: SandboxSpawner,
        extra_env: dict[str, str] | None = None,
        warm_pool: Any | None = None,
    ) -> None:
        super().__init__()
        self._spawner = spawner
        self._extra_env = extra_env or {}
        self._warm_pool = warm_pool

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
                    warm_pool=self._warm_pool,
                )
            proc = self._sessions[session_id]
            assert isinstance(proc, CCISessionProcess)
            return proc


# Keep module identity stable for isinstance checks elsewhere (mirrors the
# pattern container.py uses to spoof its __module__).
CCIRunner.__module__ = "kimi_cli.web.runner.process"


__all__ = ["CCISessionProcess", "CCIRunner", "build_warm_sandbox_env"]
