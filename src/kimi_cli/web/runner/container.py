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
import os
from typing import Any
from uuid import UUID, uuid4

from kimi_cli import logger
from kimi_cli.utils.subprocess_env import get_clean_env
from kimi_cli.web.runner.process import KimiCLIRunner, RestartWorkersSummary, SessionProcess

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

        # Mount shared data volume
        share_dir = os.environ.get("KIMI_SHARE_DIR", "/data/sessions")
        cmd.extend(["-v", f"session-data:{share_dir}"])

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

    async def get_or_create_session(self, session_id: UUID) -> SessionProcess:
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
            return self._sessions[session_id]


ContainerRunner.__module__ = "kimi_cli.web.runner.process"
