"""Internal endpoint that ships static sandbox assets to CCI Pod workers.

# hechun-fork-cci

CCI serverless Pods cannot bind-mount the gateway host's static files (the
docker path mounts ``~/.kimi/agents`` etc. via ``container.py``; the CCI Pod
spec in ``cci.py:_build_pod_spec`` has env only, no volumes). The worker still
resolves the ``SUBAGENT`` yaml off local disk (``agentspec.resolve_subagent_yaml``),
so the files must physically exist inside the Pod.

This endpoint streams a single tar of a fixed set of ``$HOME``-relative static
directories. The CCI worker downloads it on startup (see
``web/runner/worker.py``) and unpacks under ``$HOME`` before agent resolution,
restoring exactly what a docker bind-mount would have provided.

Generalised on purpose: ``_BUNDLE_DIRS`` lists every directory to pack. Today
it is just ``~/.kimi/agents`` (where ``diabetes-expert`` lands); to ship more
static assets later (e.g. a knowledge base), **add another entry to
``_BUNDLE_DIRS``** — no other change required here or in the worker.

Auth: this path is NOT under ``/api/`` so ``AuthMiddleware`` does not gate it;
the handler therefore verifies the same gateway session token
(``KIMI_WEB_SESSION_TOKEN`` → ``app.state.session_token``) itself via
``verify_token`` (Bearer header or ``?token=``). When no session token is
configured (local/dev), the endpoint is open just like the rest of the API in
that mode.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from kimi_cli import logger
from kimi_cli.web.auth import extract_token_from_request, verify_token

# Directories to pack into the sandbox-assets bundle, expressed relative to
# ``$HOME``. The tar stores each member under this relative path so a worker
# unpacking at ``$HOME`` restores the original layout.
#
# To ship additional static assets to CCI sandboxes later (knowledge base,
# extra skill bundles, ...), append the new ``$HOME``-relative directory here.
_BUNDLE_DIRS: list[str] = [
    ".kimi/agents",  # custom agent specs (diabetes-expert.yaml + system prompt)
    ".kimi/memory/knowledge",  # knowledge base tree (index.md + wiki/, packed recursively)
]

# Bundle members under this prefix are the knowledge base. The worker unpacks
# them under the session ``work_dir`` (not ``$HOME``) so ``load_knowledge_base``
# and the agent's ReadFile (both resolve relative to ``work_dir``) find them;
# everything else (``.kimi/agents``) unpacks under ``$HOME`` where
# ``discover_user_agent_specs`` also searches. Kept here so the worker's
# split-extract stays aligned with what this side packs.
KNOWLEDGE_BUNDLE_PREFIX = ".kimi/memory/knowledge"

# Endpoint path. Kept as a module constant so the env-injection side
# (cci_process._build_sandbox_env) can be aligned without string drift.
SANDBOX_ASSETS_PATH = "/internal/sandbox-assets/bundle.tar"

router = APIRouter(tags=["internal"])


def build_assets_tar(home: Path) -> bytes:
    """Pack ``_BUNDLE_DIRS`` (relative to *home*) into an in-memory tar.

    Missing or empty directories are simply skipped (the result may be an empty
    tar) — a fresh deployment without custom agents is not an error.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel in _BUNDLE_DIRS:
            abs_dir = home / rel
            if not abs_dir.is_dir():
                continue
            # ``arcname=rel`` stores members under the $HOME-relative path so
            # the worker can extract straight into $HOME.
            tar.add(str(abs_dir), arcname=rel, recursive=True)
    return buf.getvalue()


@router.get(SANDBOX_ASSETS_PATH, include_in_schema=False)
async def get_sandbox_assets_bundle(request: Request) -> Response:
    """Stream the sandbox static-asset bundle to an authenticated Pod worker."""
    expected_token = getattr(request.app.state, "session_token", None)
    if expected_token:
        provided = extract_token_from_request(request)
        if not verify_token(provided, expected_token):
            raise HTTPException(status_code=401, detail="Unauthorized")

    data = build_assets_tar(Path.home())
    logger.info(
        "[sandbox-assets] serving bundle ({size} bytes, dirs={dirs})",
        size=len(data),
        dirs=_BUNDLE_DIRS,
    )
    return Response(content=data, media_type="application/x-tar")


__all__ = ["router", "build_assets_tar", "SANDBOX_ASSETS_PATH"]
