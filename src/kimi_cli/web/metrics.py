"""Gateway /metrics endpoint for the CCI elastic deployment (spec §7.2).

# hechun-fork-cci

Exposes the spec §7.1/§7.2 metric set via ``prometheus_client``, scraped every
15s by the existing monitoring stack (``deploy/server-monitoring/`` Prometheus →
Grafana → feishu-adapter, spec §7.3). Basic-auth gated, internal network only
(same套路 as backend ``/actuator/prometheus``).

Single endpoint — NO separate cci-exporter container (spec §7.1 关键简化): the
gateway already holds spawn / warmpool / active runtime data, and the IP-pool
status is polled from ``CciRestClient.read_network`` on demand here.

Metric set (spec §7.2):
    kimo_sandbox_spawn_total{backend,result}            counter
    kimo_sandbox_spawn_duration_seconds{backend}        histogram
    kimo_warmpool_size{phase}                           gauge
    kimo_warmpool_hit_ratio                             gauge
    kimo_warmpool_acquire_duration_seconds              histogram
    kimo_active_sandboxes                               gauge
    kimo_cci_api_error_total{operation,code}            counter
    kimo_cci_network_ip_status                          gauge (0 Ready/1 IPInsufficient/2 Failed)
"""

from __future__ import annotations

import contextlib
import os
import secrets
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request, Response
from fastapi.responses import PlainTextResponse

if TYPE_CHECKING:
    from kimi_cli.web.spawner.cci_client import CciRestClient

# ── metric definitions (module-level singletons; instrument from call sites) ──
# Imported lazily so the docker path doesn't require prometheus_client unless
# /metrics is actually mounted.


def _build_metrics():
    from prometheus_client import (  # noqa: PLC0415
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
    )

    registry = CollectorRegistry()
    metrics = {
        "spawn_total": Counter(
            "kimo_sandbox_spawn_total",
            "Sandbox spawn attempts",
            ["backend", "result"],
            registry=registry,
        ),
        "spawn_duration": Histogram(
            "kimo_sandbox_spawn_duration_seconds",
            "Sandbox spawn duration",
            ["backend"],
            registry=registry,
        ),
        "warmpool_size": Gauge(
            "kimo_warmpool_size",
            "Warm pool size by phase",
            ["phase"],
            registry=registry,
        ),
        "warmpool_hit_ratio": Gauge(
            "kimo_warmpool_hit_ratio",
            "Warm pool acquire hit ratio",
            registry=registry,
        ),
        "warmpool_acquire_duration": Histogram(
            "kimo_warmpool_acquire_duration_seconds",
            "Warm pool acquire duration",
            registry=registry,
        ),
        "active_sandboxes": Gauge(
            "kimo_active_sandboxes",
            "Currently active sandboxes",
            registry=registry,
        ),
        "cci_api_error_total": Counter(
            "kimo_cci_api_error_total",
            "CCI REST API errors",
            ["operation", "code"],
            registry=registry,
        ),
        "cci_network_ip_status": Gauge(
            "kimo_cci_network_ip_status",
            "CCI default-network IP pool status (0 Ready / 1 IPInsufficient / 2 Failed)",
            registry=registry,
        ),
        # hechun-fork-cci: a worker that was REQUIRED to load a specific agent
        # (gateway forwarded SUBAGENT + KIMI_REQUIRE_AGENT) but couldn't resolve
        # it refuses to start (fail-fast, never serves users the wrong agent) and
        # exits with AGENT_LOAD_FAILURE_EXIT_CODE. The gateway counts each such
        # exit here so a mis-mounted skill bundle is visible on the dashboard
        # immediately instead of silently degrading. ``reason`` ∈
        # {subagent_unresolved, agent_required_missing}.
        "agent_load_failure_total": Counter(
            "kimo_agent_load_failure_total",
            "Sandbox worker refused to start because its required agent failed to load",
            ["reason"],
            registry=registry,
        ),
    }
    return registry, metrics


# Network status → gauge value mapping (spec §7.2 / §4.2).
_IP_STATUS_READY = 0
_IP_STATUS_INSUFFICIENT = 1
_IP_STATUS_FAILED = 2


def network_status_to_gauge(status: str | None) -> int:
    """Map ``Network.status`` to the gauge value (spec §4.2 IPInsufficient)."""
    if status == "IPInsufficient":
        return _IP_STATUS_INSUFFICIENT
    if status in (None, "", "Ready", "Active"):
        return _IP_STATUS_READY
    return _IP_STATUS_FAILED


class MetricsState:
    """Holds the registry + metric handles; lives on ``app.state.metrics``."""

    def __init__(self) -> None:
        self.registry, self.metrics = _build_metrics()
        # Optional wiring (set during startup, spec §7.1 single-endpoint poll):
        self.cci_client: CciRestClient | None = None
        self.namespace: str = os.environ.get("HUAWEICLOUD_CCI_NAMESPACE", "kalinin-test")
        self.network_name: str = os.environ.get(
            "HUAWEICLOUD_CCI_NETWORK", "default-network"
        )

    async def refresh_ip_status(self) -> None:
        """Poll Network status and update the IP-pool gauge (spec §4.2/§7.2)."""
        if self.cci_client is None:
            return
        try:
            net = await self.cci_client.read_network(self.namespace, self.network_name)
            status = net.get("status")
            if isinstance(status, dict):
                # k8s-style status object: pull a phase/condition-ish field.
                status = status.get("phase") or status.get("status")
            value = network_status_to_gauge(status if isinstance(status, str) else None)
        except Exception:  # noqa: BLE001 — metrics must never crash the gateway
            value = _IP_STATUS_FAILED
        self.metrics["cci_network_ip_status"].set(value)

    async def refresh_active_sandboxes(self) -> None:
        """Set ``kimo_active_sandboxes`` from the REAL Pod count (drift-immune).

        The inc/dec call-site bookkeeping (spawn +1 / stop -1) drifts whenever a
        Pod vanishes outside the gateway's stop path — out-of-band delete, Pod
        self-death, or gateway restart (in-memory gauge survives but Pods don't).
        Polling the actual ``kimo-sandbox-*`` Pod count on every scrape keeps the
        gauge truthful regardless. None-safe; never crashes the scrape.
        """
        if self.cci_client is None:
            return
        try:
            pods = await self.cci_client.list_pods(self.namespace)
            count = sum(
                1
                for p in pods
                if str(p.get("metadata", {}).get("name", "")).startswith("kimo-sandbox-")
            )
            self.metrics["active_sandboxes"].set(count)
        except Exception:  # noqa: BLE001 — metrics must never crash the gateway
            pass

    # ── runtime instrumentation helpers (spec §7.2) ─────────────────────────────
    # Call-site sugar so the CCI spawn / stop / api-error paths can emit metrics
    # without touching prometheus_client handles directly. Every helper swallows
    # its own exceptions: instrumentation must NEVER alter spawn behaviour or
    # crash the gateway (spec §7.1). These are no-ops on the docker path because
    # MetricsState is only constructed when KIMI_METRICS_ENABLED — call sites
    # guard against ``self.metrics is None`` before reaching here.

    def record_spawn(self, *, backend: str, result: str) -> None:
        """``kimo_sandbox_spawn_total{backend,result}`` +1 (one per spawn attempt)."""
        with contextlib.suppress(Exception):
            self.metrics["spawn_total"].labels(backend=backend, result=result).inc()

    def observe_spawn_duration(self, *, backend: str, seconds: float) -> None:
        """``kimo_sandbox_spawn_duration_seconds{backend}`` observe one spawn."""
        with contextlib.suppress(Exception):
            self.metrics["spawn_duration"].labels(backend=backend).observe(seconds)

    def inc_active_sandboxes(self, delta: int = 1) -> None:
        """``kimo_active_sandboxes`` += delta (spawn success +1 / stop -1)."""
        with contextlib.suppress(Exception):
            self.metrics["active_sandboxes"].inc(delta)

    def record_cci_api_error(self, *, operation: str, code: str) -> None:
        """``kimo_cci_api_error_total{operation,code}`` +1 on a CCI REST error."""
        with contextlib.suppress(Exception):
            self.metrics["cci_api_error_total"].labels(operation=operation, code=code).inc()

    def record_agent_load_failure(self, *, reason: str) -> None:
        """``kimo_agent_load_failure_total{reason}`` +1 when a worker refused to
        start because its required agent could not be loaded.

        ``reason`` is one of ``subagent_unresolved`` (``SUBAGENT`` was set but no
        matching yaml on the Pod) or ``agent_required_missing``
        (``KIMI_REQUIRE_AGENT`` was set but resolution still landed on the
        default agent). Called from the gateway when it sees a worker exit with
        ``AGENT_LOAD_FAILURE_EXIT_CODE``."""
        with contextlib.suppress(Exception):
            self.metrics["agent_load_failure_total"].labels(reason=reason).inc()


def _basic_auth_ok(request: Request, user: str, password: str) -> bool:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return False
    import base64  # noqa: PLC0415

    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        got_user, _, got_pw = decoded.partition(":")
    except Exception:  # noqa: BLE001
        return False
    # Constant-time compare to avoid trivial timing oracles.
    return secrets.compare_digest(got_user, user) and secrets.compare_digest(got_pw, password)


def build_metrics_router() -> APIRouter:
    """Build the ``/metrics`` router with basic-auth (spec §7.2).

    Credentials come from ``KIMI_METRICS_BASIC_AUTH_USER`` /
    ``KIMI_METRICS_BASIC_AUTH_PASSWORD`` (set in ``secrets.yml``, spec §7.3-5).
    When unset, auth is disabled (dev convenience; production must set them and
    the endpoint is only reachable on the internal ``hechun_hechun-net``).
    """
    router = APIRouter()

    @router.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(request: Request) -> Response:  # pyright: ignore[reportUnusedFunction]
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest  # noqa: PLC0415

        user = os.environ.get("KIMI_METRICS_BASIC_AUTH_USER")
        password = os.environ.get("KIMI_METRICS_BASIC_AUTH_PASSWORD")
        if user and password and not _basic_auth_ok(request, user, password):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="kimo-metrics"'},
            )

        state = getattr(request.app.state, "metrics", None)
        if state is None:
            return PlainTextResponse("# metrics not initialized\n", status_code=503)
        await state.refresh_ip_status()
        await state.refresh_active_sandboxes()
        data = generate_latest(state.registry)
        return Response(content=data, media_type=CONTENT_TYPE_LATEST)

    return router


__all__ = [
    "MetricsState",
    "build_metrics_router",
    "network_status_to_gauge",
]
