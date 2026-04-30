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
from kimi_cli.web.runner.process import KimiCLIRunner, SessionProcess


def _read_owner_id_from_disk(session_id: UUID) -> str | None:
    """Locate the session's ``state.json`` and return its ``owner_id`` field.

    Sessions live under ``$KIMI_SHARE_DIR/sessions/<work_dir_hash>/<session_id>/``;
    the work-dir hash is unknown to the runner, so we glob across hashes.
    """
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
    "LLM_PROVIDER",
    "LLM_THINKING",
    "LLM_TEMPERATURE",
    # Session / runtime
    "KIMI_SHARE_DIR",
    "KIMI_SESSIONS_DIR",
    "KIMI_WORK_DIR",
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
