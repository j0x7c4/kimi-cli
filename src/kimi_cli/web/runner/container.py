"""Containerized session process management using Docker.

This module provides ContainerSessionProcess and ContainerRunner, which
replace the local subprocess model with per-session Docker containers.

Communication model:
- Each session runs in its own Docker container (kimi-agent-sandbox image)
- The gateway executes ``docker run -i --rm`` as an asyncio subprocess
- stdin/stdout pipes are used for JSON-RPC communication (same protocol)
- Container lifecycle (start/stop/remove) is managed via the subprocess
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from kimi_cli import logger
from kimi_cli.memory import resolve_owner_id
from kimi_cli.utils.subprocess_env import get_clean_env
from kimi_cli.web.api.sandbox_assets import SANDBOX_ASSETS_PATH
from kimi_cli.web.runner.process import KimiCLIRunner, SessionProcess


def _read_owner_id_from_disk(session_id: UUID) -> str | None:
    """Resolve the session's ``owner_id``, preferring the active storage backend.

    Lookup order (M4 §2.4.2.C/J):
      1. Active ``KimoStorage`` (PgKimoStorage when ``KIMI_STORAGE_BACKEND=postgres``,
         else FileKimoStorage) — single source of truth for session state under
         postgres backend; ``state.json`` on disk is **stale** because
         ``sessions.py:create_session`` writes ``owner_id`` only through
         ``save_session_state_via_storage`` after the priority-resolution block.
      2. Disk fallback: glob ``state.json`` under ``$KIMI_SHARE_DIR/sessions/*/<sid>/``
         for backward-compat with file mode / older test harnesses that didn't
         wire ``kimo_storage`` into app state.

    Function name kept (``_from_disk``) to avoid churning the call site signature;
    behavior is now "from storage preferred, disk fallback".
    """
    # 1. Storage abstraction (Pg in postgres mode, File in file mode)
    try:
        from kimi_cli.storage import build_storage  # local import to avoid cycle

        storage = build_storage()
        state = storage.load_session_state(session_id)
        if state is not None and state.owner_id:
            return state.owner_id
    except Exception as e:  # noqa: BLE001
        # Storage init failure must not block sandbox spawn — fall through to
        # disk lookup so the worst case is anonymous fallback, not a hard crash.
        logger.warning(
            "[container] storage lookup failed sid={sid} err={err}; falling back to disk",
            sid=session_id,
            err=e,
        )

    # 2. Disk fallback (file mode / pre-storage sessions)
    share = os.environ.get("KIMI_SHARE_DIR")
    if not share:
        return None
    sessions_root = Path(share) / "sessions"
    if not sessions_root.is_dir():
        return None
    for state_file in sessions_root.glob(f"*/{session_id}/state.json"):
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        owner = data.get("owner_id")
        return owner if isinstance(owner, str) else None
    return None


def _read_subagent_from_disk(session_id: UUID) -> str | None:
    """Read the ``subagent`` field from this session's ``session_config.json``.

    Used by the container runner to forward a ``SUBAGENT`` env var into the
    sandbox so kimi-cli can pick the right agent yaml at startup.  Mirrors
    the disk-glob pattern of :func:`_read_owner_id_from_disk` because the
    work-dir hash is unknown here.
    """
    share = os.environ.get("KIMI_SHARE_DIR")
    if not share:
        return None
    sessions_root = Path(share) / "sessions"
    if not sessions_root.is_dir():
        return None
    for cfg_file in sessions_root.glob(f"*/{session_id}/session_config.json"):
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        value = data.get("subagent")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None
    return None


def _read_agent_name_from_disk(session_id: UUID) -> str | None:
    """Resolve the agent NAME to forward as ``SUBAGENT`` into a CCI Pod.

    Returns the ``subagent`` field if set, else the stem of ``agent_spec_path``
    (e.g. ``/root/.kimi/agents/diabetes-expert.yaml`` → ``diabetes-expert``).

    Why CCI needs this but docker doesn't: the docker sandbox bind-mounts the
    session dir, so the worker reads ``agent_spec_path`` straight out of
    ``session_config.json``.  The CCI worker constructs a *fresh* session on the
    Pod (no ``session_config.json`` there — :func:`run_worker` falls back to
    ``KimiCLISession.create``), so the gateway must forward the agent *name* and
    let the worker resolve it by name against the downloaded ``~/.kimi/agents``
    bundle.  ``agent_name`` sessions land as ``agent_spec_path`` (not
    ``subagent``) in the config, so :func:`_read_subagent_from_disk` returns
    ``None`` for them — this helper covers that case.
    """
    share = os.environ.get("KIMI_SHARE_DIR")
    if not share:
        return None
    sessions_root = Path(share) / "sessions"
    if not sessions_root.is_dir():
        return None
    for cfg_file in sessions_root.glob(f"*/{session_id}/session_config.json"):
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        sub = data.get("subagent")
        if isinstance(sub, str) and sub.strip():
            return sub.strip()
        spec_path = data.get("agent_spec_path")
        if isinstance(spec_path, str) and spec_path.strip():
            return Path(spec_path.strip()).stem
        return None
    return None


# Environment variable names to forward into sandbox containers
_SANDBOX_ENV_VARS = [
    # LLM configuration
    "KIMI_API_KEY",
    "KIMI_BASE_URL",
    "KIMI_MODEL_NAME",
    "KIMI_MODEL_MAX_CONTEXT_SIZE",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "LLM_PROVIDERS",
    "LLM_DEFAULT_PROVIDER",
    "LLM_PROVIDER",
    "LLM_THINKING",
    "LLM_TEMPERATURE",
    # Session / runtime
    "KIMI_SHARE_DIR",
    "KIMI_SESSIONS_DIR",
    "KIMI_WORK_DIR",
    # M4 §2.4.2.J: storage backend 切换 env 必须 forward 到 sandbox 容器，
    # 否则 sandbox 内 PgKimoStorage 拿不到 KIMO_DB_URL 无法工作
    # （gateway 切 KIMI_STORAGE_BACKEND=postgres 后，sandbox 仍按 file 跑会
    # 导致 archivist 写 ai_user_memory 不生效）。dev 默认 file 模式三个 var
    # 留空 / "file" 也无副作用。
    "KIMI_STORAGE_BACKEND",
    "KIMO_DB_URL",
    "KIMO_DB_POOL_SIZE",
    # Feature flags
    "ENABLE_BROWSER",
    "ENABLE_JUPYTER",
    "ENABLE_SHELL_SANDBOX",
    "BLOCK_DANGEROUS_COMMANDS",
    # Browser / display
    "DISPLAY",
    "SCREEN_RESOLUTION",
    "CHROME_LOCALE",
    "TZ",
    "CHROME_INIT_URL",
    "CHROME_FLAGS",
    "USE_CDP",
    # HuggingFace
    "HF_HOME",
    "HF_TOKEN",
    "HUGGINGFACE_HUB_CACHE",
    "TRANSFORMERS_CACHE",
    # hechun (avocado) integration -- see custom-skills/hechun/README.md.
    # ``HECHUN_INTERNAL_BASE_URL`` and ``INTERNAL_API_TOKEN`` let hechun
    # HTTP-runtime skills (bolus_calc, bg_interpret, ...) reach the backend.
    # ``HECHUN_MCP_URL`` and ``HECHUN_MCP_TOKEN`` are consumed by the MCP
    # client config that exposes the 10 read-only data tools to the LLM.
    # ``KIMI_USER_ID`` is already injected from disk-resolved owner_id
    # elsewhere in this file; the values above are passed through from
    # the gateway process env so the same upstream knobs work in both
    # local and container mode.
    "HECHUN_INTERNAL_BASE_URL",
    "INTERNAL_API_TOKEN",
    "HECHUN_MCP_URL",
    "HECHUN_MCP_TOKEN",
    # CCI fresh session 的 yolo 兜底：storage 读回失败时 worker.py 从此 env 把 approval.yolo
    # 置 True（否则 headless iOS/Flutter session 的工具调用卡在 approval 无限等待）。
    "KIMO_DEFAULT_YOLO",
]


class ContainerSessionProcess(SessionProcess):
    """SessionProcess that runs the worker inside a Docker container.

    The container is launched via ``docker run -i --rm`` as an asyncio
    subprocess.  Killing the subprocess stops and removes the container
    automatically.  stdin/stdout communication is identical to the local
    subprocess model.
    """

    def __init__(
        self,
        session_id: UUID,
        *,
        image: str = "kimi-agent-sandbox:latest",
        network: str | None = None,
        resource_limits: dict[str, str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        super().__init__(session_id)
        self._image = image
        self._network = network
        self._resource_limits = resource_limits or {}
        self._extra_env = extra_env or {}
        self._container_name = f"kimi-session-{session_id}"

    @property
    def container_name(self) -> str:
        """Current container name (updated on each start)."""
        return self._container_name

    async def start(
        self,
        *,
        reason: str | None = None,
        detail: str | None = None,
        restart_started_at: float | None = None,
    ) -> None:
        """Start the KimiCLI worker inside a Docker container."""
        async with self._lock:
            if self.is_alive:
                if self._read_task is None or self._read_task.done():
                    self._read_task = asyncio.create_task(self._read_loop())
                return

            self._in_flight_prompt_ids.clear()
            self._expecting_exit = False
            self._worker_id = str(uuid4())

            # Use worker_id suffix to avoid container name conflicts when
            # restarting: the old container may still be stopping (--rm is
            # async) when the new one tries to register the same name.
            self._container_name = f"kimi-session-{self.session_id}-{self._worker_id[:8]}"

            STREAM_LIMIT = 16 * 1024 * 1024

            cmd = self._build_docker_cmd()

            logger.info(
                "Starting container for session {session_id}: {cmd}",
                session_id=self.session_id,
                cmd=" ".join(cmd),
            )

            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=STREAM_LIMIT,
                env=get_clean_env(),
            )

            self._read_task = asyncio.create_task(self._read_loop())
            if restart_started_at is not None:
                import time

                elapsed_ms = int((time.perf_counter() - restart_started_at) * 1000)
                detail = f"restart_ms={elapsed_ms}"
                await self._emit_status("idle", reason=reason or "start", detail=detail)
                await self._emit_restart_notice(reason=reason, restart_ms=elapsed_ms)
            else:
                await self._emit_status("idle", reason=reason or "start", detail=None)

    def _build_docker_cmd(self) -> list[str]:
        """Build the ``docker run`` command for this session."""
        cmd: list[str] = [
            "docker",
            "run",
            "-i",
            "--rm",
            "--name",
            self._container_name,
        ]

        # Resource limits
        cpus = self._resource_limits.get("cpus")
        if cpus:
            cmd.extend(["--cpus", cpus])
        memory = self._resource_limits.get("memory")
        if memory:
            cmd.extend(["--memory", memory])
        pids = self._resource_limits.get("pids")
        if pids:
            cmd.extend(["--pids-limit", pids])

        # Network
        if self._network:
            cmd.extend(["--network", self._network])

        # Mount shared data volume.
        # KIMI_SESSION_DATA_DIR is the host-side path (bind mount).
        # KIMI_SHARE_DIR is the in-container mount point.
        # We use the host path so that sandbox containers (spawned via the
        # Docker socket from inside the gateway container) see the same data
        # as the gateway itself.
        share_dir = os.environ.get("KIMI_SHARE_DIR", "/data/sessions")
        host_share_dir = os.environ.get("KIMI_SESSION_DATA_DIR", share_dir)
        cmd.extend(["-v", f"{host_share_dir}:{share_dir}"])

        # Mount custom skills directory if configured
        custom_skills = os.environ.get("CUSTOM_SKILLS_HOST_PATH")
        if custom_skills:
            cmd.extend(["-v", f"{custom_skills}:/root/.config/agents/skills:ro"])

        # hechun-fork: docker 模式复用 CCI bundle 下发（KIMO_GATEWAY_INTERNAL_URL 门控）。
        # 非空 => 走 bundle 下发：worker 启动时把 agents/knowledge 解包到 $HOME/work_dir，
        # 此时不能再把 ~/.kimi/agents 只读 bind-mount（会与解包写入冲突），与 CCI「全下载、
        # 零挂载」保持一致。空 => dev/未配 gateway 内网地址，行为与改动前完全一致。
        internal_url = (os.environ.get("KIMO_GATEWAY_INTERNAL_URL") or "").strip()

        # Mount user agent specs directory (for subagent yaml discovery).
        # kimi-cli's agentspec.discover() searches ~/.kimi/agents at sandbox startup.
        # Skipped under bundle mode (internal_url set): the worker unpacks agents itself.
        custom_agents = os.environ.get("CUSTOM_AGENTS_HOST_PATH")
        if custom_agents and not internal_url:
            cmd.extend(["-v", f"{custom_agents}:/root/.kimi/agents:ro"])

        # Mount HuggingFace cache directory if configured
        hf_cache = os.environ.get("HF_CACHE_HOST_PATH")
        if hf_cache:
            cmd.extend(["-v", f"{hf_cache}:/root/.cache/huggingface"])
            cmd.extend(["-e", "HF_HOME=/root/.cache/huggingface"])

        # Forward known environment variables
        for var_name in _SANDBOX_ENV_VARS:
            value = os.environ.get(var_name)
            if value is not None:
                cmd.extend(["-e", f"{var_name}={value}"])

        # Extra env vars from configuration
        for key, value in self._extra_env.items():
            cmd.extend(["-e", f"{key}={value}"])

        # Security: drop privileges
        cmd.extend(["--privileged=false"])

        # Pass session ID so start-sandbox.sh knows which worker to launch
        cmd.extend(["-e", f"KIMI_SESSION_ID={self.session_id}"])

        # Pass user identity so the agent can route per-user private memory
        # (cross-session highlights / persistent.jsonl) under
        # ``$KIMI_SHARE_DIR/users/<owner_id>/memory/``.  Sessions without an
        # authenticated owner fall back to a sentinel so they cannot pollute
        # real users' data.
        owner_id_raw = _read_owner_id_from_disk(self.session_id)
        cmd.extend(["-e", f"KIMI_USER_ID={resolve_owner_id(owner_id_raw)}"])

        # Forward per-session ``subagent`` selection (e.g. hechun avocado picks
        # ``diabetes-expert``).  kimi-cli inside the sandbox is expected to
        # pick the matching ``custom-skills/hechun/subagents/<name>.yaml`` at
        # startup.  When unset, sandbox falls back to the default agent.
        subagent = _read_subagent_from_disk(self.session_id)
        if subagent:
            cmd.extend(["-e", f"SUBAGENT={subagent}"])

        # hechun-fork: docker 模式复用 CCI bundle 下发（KIMO_GATEWAY_INTERNAL_URL 门控）。
        # internal_url 非空时注入 assets 下载 URL + token，worker 启动时从 gateway 的
        # sandbox-assets 端点拉 bundle（agents + .kimi/memory/knowledge）解包。URL/token 由
        # gateway 组装，非 _SANDBOX_ENV_VARS 直通。空时不注入 => 与改动前一致（走 bind-mount）。
        if internal_url:
            assets_url = internal_url.rstrip("/") + SANDBOX_ASSETS_PATH
            cmd.extend(["-e", f"KIMO_SANDBOX_ASSETS_URL={assets_url}"])
            token = os.environ.get("KIMI_WEB_SESSION_TOKEN")
            if token:
                cmd.extend(["-e", f"KIMO_SANDBOX_ASSETS_TOKEN={token}"])

        # Image + entrypoint command (runs start-sandbox.sh which launches
        # Xvfb, kernel server, browser guard, and finally the worker)
        cmd.extend([self._image, "/start-sandbox.sh"])

        return cmd


class ContainerRunner(KimiCLIRunner):
    """Manages multiple session processes inside Docker containers."""

    def __init__(
        self,
        *,
        image: str = "kimi-agent-sandbox:latest",
        network: str | None = None,
        resource_limits: dict[str, str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._image = image
        self._network = network
        self._resource_limits = resource_limits or {}
        self._extra_env = extra_env or {}

    def start(self) -> None:
        """Start the runner (no-op, containers started on demand)."""
        pass

    def get_session(self, session_id: UUID) -> ContainerSessionProcess | None:
        """Return the session process for the given session ID, or None."""
        proc = self._sessions.get(session_id)
        if proc is None:
            return None
        assert isinstance(proc, ContainerSessionProcess)
        return proc

    async def get_or_create_session(self, session_id: UUID) -> ContainerSessionProcess:
        """Get or create a containerized session process."""
        async with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = ContainerSessionProcess(
                    session_id,
                    image=self._image,
                    network=self._network,
                    resource_limits=self._resource_limits,
                    extra_env=self._extra_env,
                )
            proc = self._sessions[session_id]
            assert isinstance(proc, ContainerSessionProcess)
            return proc


ContainerRunner.__module__ = "kimi_cli.web.runner.process"
