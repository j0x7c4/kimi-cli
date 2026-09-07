"""Internal warm-pool endpoints (health probe + status) — hechun fork.

# hechun-fork-cci (warm pool, plan §0.5)

The backend scheduler owns *when* the pool is checked (it already runs a 60s
sweep); only the gateway can actually check it, because only the gateway holds
the CCI client and the exec streams into the Pods. So the backend calls
``POST /warmpool/health-check`` and this module does the real work.

🔴 What "health" means here: a probe that reaches the **worker**, not the Pod.
Since the pool never evicts by age, this is the only eviction signal there is,
and a Pod can be ``Running`` while its worker is wedged or missing its agent
spec — such a Pod would sit in the pool forever and fail every claim. The probe
therefore uses the warm handshake's own ping/pong (the wire protocol has no
ping, and a pre-bind worker has no soul to answer JSON-RPC at all).

Auth: like ``sandbox_assets``, this path is not under ``/api/`` so
``AuthMiddleware`` does not gate it; the handlers verify the gateway session
token themselves.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from kimi_cli.web.auth import extract_token_from_request, verify_token

router = APIRouter(tags=["warmpool"])

WARMPOOL_HEALTH_PATH = "/warmpool/health-check"
WARMPOOL_STATUS_PATH = "/warmpool/status"


def _require_token(request: Request) -> None:
    expected = getattr(request.app.state, "session_token", None)
    if not expected:
        return  # local/dev: same open posture as the rest of the API
    if not verify_token(extract_token_from_request(request), expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.post(WARMPOOL_HEALTH_PATH)
async def warmpool_health_check(request: Request) -> dict[str, Any]:
    """Probe every pooled Pod at the worker layer; evict + refill the dead ones.

    ``{"enabled": false}`` (HTTP 200) when the pool is off — the caller polls
    this on a schedule and a disabled pool is a configuration state, not an
    error.
    """
    _require_token(request)
    pool = getattr(request.app.state, "warm_pool", None)
    if pool is None:
        return {"enabled": False}
    result: dict[str, Any] = await pool.probe()
    result["enabled"] = True
    return result


@router.get(WARMPOOL_STATUS_PATH)
async def warmpool_status(request: Request) -> dict[str, Any]:
    """Cheap read-only view (no probing): pool size, hits, misses, backoff."""
    _require_token(request)
    pool = getattr(request.app.state, "warm_pool", None)
    if pool is None:
        return {"enabled": False}
    stats: dict[str, Any] = pool.stats()
    return stats
