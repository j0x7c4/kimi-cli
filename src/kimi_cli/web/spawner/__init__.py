"""Sandbox spawner abstraction (hechun fork; CCI elastic deployment).

# hechun-fork-cci ─────────────────────────────────────────────────────────────
# This whole package is hechun-fork private (华为云 CCI 2.0 弹性部署落地).
# Upstream kimi-cli has no spawner abstraction — the gateway runs sessions either
# as local subprocesses (``KimiCLIRunner``) or via ``docker run -i --rm``
# (``ContainerRunner`` in ``web/runner/container.py``). This package adds a thin
# ``SandboxSpawner`` Protocol + ``SandboxHandle`` value object so the CCI backend
# (and the existing docker model) can be selected at startup by
# ``KIMI_SPAWNER_BACKEND``. Keep all CCI code under this package to ease future
# upstream rebases.
#
# Design source of truth:
#   docs/superpowers/specs/2026-06-24-kimo-cci-api-landing-design.md  (§1.3/§5/§6)
# ──────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID


@dataclass(slots=True)
class SandboxHandle:
    """Opaque handle to a spawned sandbox (spec §1.3, shape preserved).

    ``backend``           — "docker" | "cci".
    ``handle_id``         — backend-native id (docker container name / CCI pod name).
    ``network_endpoint``  — where the gateway connects to drive the worker
                            (e.g. ``ws://<pod-ip>:5494`` under CCI).
    ``created_at``        — spawn timestamp (UTC), used for cost / age accounting.
    ``meta``              — backend-specific extras (pod ip, labels, …).
    """

    backend: str
    handle_id: str
    network_endpoint: str | None = None
    created_at: datetime | None = None
    meta: dict[str, str] = field(default_factory=dict)


class SpawnTimeout(RuntimeError):
    """Raised when a sandbox does not reach Running within the spawn timeout."""

    def __init__(self, name: str):
        super().__init__(f"sandbox {name!r} did not reach Running before timeout")
        self.name = name


@runtime_checkable
class SandboxSpawner(Protocol):
    """Abstract lifecycle for a single-session sandbox (spec §1.3, unchanged shape).

    All methods are async. ``attach`` returns a duck-typed stream object that the
    upstream ``WSStreamProxy`` drives exactly like a docker
    ``container.attach_socket()`` — see :class:`kimi_cli.web.spawner.cci_exec.KimoExecStream`.
    """

    async def spawn(self, sid: UUID, owner_id: str, env: dict[str, str]) -> SandboxHandle: ...

    async def attach(self, handle: SandboxHandle) -> object: ...

    async def stop(self, handle: SandboxHandle) -> None: ...

    async def healthcheck(self, handle: SandboxHandle) -> bool: ...


def build_spawner() -> SandboxSpawner | None:
    """Construct the configured spawner backend.

    Selected by env ``KIMI_SPAWNER_BACKEND`` ∈ {``docker`` (default), ``cci``}.

    - ``docker`` → returns ``None``; the gateway keeps using the existing
      ``ContainerRunner`` / ``KimiCLIRunner`` path (no behaviour change). The CCI
      backend is purely additive.
    - ``cci``    → :class:`kimi_cli.web.spawner.cci.CCISpawner`, configured from
      ``HUAWEICLOUD_*`` env (see ``cci.CCISpawner.from_env``).

    Returning ``None`` for docker keeps this factory non-invasive: app.py only
    swaps the spawner when explicitly opted into CCI.
    """
    backend = (os.environ.get("KIMI_SPAWNER_BACKEND") or "docker").strip().lower()
    if backend == "cci":
        from kimi_cli.web.spawner.cci import CCISpawner

        return CCISpawner.from_env()

    if backend != "docker":
        from kimi_cli import logger

        logger.warning(
            "[spawner] unknown KIMI_SPAWNER_BACKEND={b!r}; using docker path", b=backend
        )
    return None


__all__ = [
    "SandboxHandle",
    "SandboxSpawner",
    "SpawnTimeout",
    "build_spawner",
]
