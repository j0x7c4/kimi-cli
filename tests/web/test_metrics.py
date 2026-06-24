"""Unit tests for web/metrics.py: metric set + IP-status mapping + basic-auth.

# hechun-fork-cci
"""

from __future__ import annotations

import base64

import pytest

pytest.importorskip("prometheus_client")

from kimi_cli.web.metrics import (  # noqa: E402
    MetricsState,
    build_metrics_router,
    network_status_to_gauge,
)


class TestNetworkStatusMapping:
    def test_ready_is_zero(self):
        assert network_status_to_gauge("Ready") == 0
        assert network_status_to_gauge(None) == 0
        assert network_status_to_gauge("Active") == 0

    def test_ip_insufficient_is_one(self):
        assert network_status_to_gauge("IPInsufficient") == 1

    def test_other_is_failed_two(self):
        assert network_status_to_gauge("Failed") == 2
        assert network_status_to_gauge("Weird") == 2


class TestMetricSet:
    def test_registry_exposes_spec_7_2_metrics(self):
        state = MetricsState()
        from prometheus_client import generate_latest

        # touch some metrics so they appear in the exposition.
        state.metrics["spawn_total"].labels(backend="cci", result="ok").inc()
        state.metrics["active_sandboxes"].set(3)
        state.metrics["warmpool_size"].labels(phase="ready").set(5)
        state.metrics["cci_api_error_total"].labels(operation="create_pod", code="500").inc()
        state.metrics["cci_network_ip_status"].set(0)

        text = generate_latest(state.registry).decode("utf-8")
        for name in (
            "kimo_sandbox_spawn_total",
            "kimo_sandbox_spawn_duration_seconds",
            "kimo_warmpool_size",
            "kimo_warmpool_hit_ratio",
            "kimo_warmpool_acquire_duration_seconds",
            "kimo_active_sandboxes",
            "kimo_cci_api_error_total",
            "kimo_cci_network_ip_status",
        ):
            assert name in text

    async def test_refresh_ip_status_reads_network(self):
        state = MetricsState()

        class _Client:
            async def read_network(self, ns, name):
                return {"status": "IPInsufficient"}

        state.cci_client = _Client()  # type: ignore[assignment]
        await state.refresh_ip_status()
        from prometheus_client import generate_latest

        text = generate_latest(state.registry).decode("utf-8")
        assert "kimo_cci_network_ip_status 1.0" in text

    async def test_refresh_ip_status_marks_failed_on_error(self):
        state = MetricsState()

        class _Client:
            async def read_network(self, ns, name):
                raise RuntimeError("network unreachable")

        state.cci_client = _Client()  # type: ignore[assignment]
        await state.refresh_ip_status()  # must not raise
        from prometheus_client import generate_latest

        text = generate_latest(state.registry).decode("utf-8")
        assert "kimo_cci_network_ip_status 2.0" in text


class _FakeRequest:
    def __init__(self, app, headers: dict[str, str]):
        self.app = app
        self.headers = headers


class _FakeApp:
    class _State:
        pass

    def __init__(self):
        self.state = _FakeApp._State()


def _get_handler():
    router = build_metrics_router()
    route = next(r for r in router.routes if getattr(r, "path", None) == "/metrics")
    return route.endpoint  # type: ignore[attr-defined]


class TestBasicAuth:
    async def test_401_when_creds_required_and_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("KIMI_METRICS_BASIC_AUTH_USER", "u")
        monkeypatch.setenv("KIMI_METRICS_BASIC_AUTH_PASSWORD", "p")
        handler = _get_handler()
        app = _FakeApp()
        app.state.metrics = MetricsState()
        resp = await handler(_FakeRequest(app, {}))
        assert resp.status_code == 401

    async def test_200_with_correct_creds(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_METRICS_BASIC_AUTH_USER", "u")
        monkeypatch.setenv("KIMI_METRICS_BASIC_AUTH_PASSWORD", "p")
        handler = _get_handler()
        app = _FakeApp()
        app.state.metrics = MetricsState()
        token = base64.b64encode(b"u:p").decode()
        resp = await handler(_FakeRequest(app, {"authorization": f"Basic {token}"}))
        assert resp.status_code == 200

    async def test_503_when_metrics_uninitialized(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KIMI_METRICS_BASIC_AUTH_USER", raising=False)
        monkeypatch.delenv("KIMI_METRICS_BASIC_AUTH_PASSWORD", raising=False)
        handler = _get_handler()
        app = _FakeApp()
        app.state.metrics = None
        resp = await handler(_FakeRequest(app, {}))
        assert resp.status_code == 503

    async def test_no_auth_required_when_creds_unset(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KIMI_METRICS_BASIC_AUTH_USER", raising=False)
        monkeypatch.delenv("KIMI_METRICS_BASIC_AUTH_PASSWORD", raising=False)
        handler = _get_handler()
        app = _FakeApp()
        app.state.metrics = MetricsState()
        resp = await handler(_FakeRequest(app, {}))
        assert resp.status_code == 200
