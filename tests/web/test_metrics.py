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

        state.metrics["agent_load_failure_total"].labels(reason="subagent_unresolved").inc()

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
            "kimo_agent_load_failure_total",
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

    async def test_refresh_active_sandboxes_counts_real_pods(self):
        state = MetricsState()

        class _Client:
            async def list_pods(self, ns, **kw):
                # 2 sandbox Pods + 1 imagesnapshot Pod + 1 unrelated → count == 2
                return [
                    {"metadata": {"name": "kimo-sandbox-aaa"}},
                    {"metadata": {"name": "kimo-sandbox-bbb"}},
                    {"metadata": {"name": "cci-imagesnapshot-xyz"}},
                    {"metadata": {"name": "some-other-pod"}},
                ]

        state.cci_client = _Client()  # type: ignore[assignment]
        await state.refresh_active_sandboxes()
        from prometheus_client import generate_latest

        text = generate_latest(state.registry).decode("utf-8")
        assert "kimo_active_sandboxes 2.0" in text

    async def test_refresh_active_sandboxes_safe_on_error(self):
        state = MetricsState()

        class _Client:
            async def list_pods(self, ns, **kw):
                raise RuntimeError("cci unreachable")

        state.cci_client = _Client()  # type: ignore[assignment]
        await state.refresh_active_sandboxes()  # must not raise

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


class TestInstrumentationHelpers:
    """spec §7.2 runtime emit helpers used by the CCI spawn / stop / error paths."""

    def test_record_spawn_and_active_and_duration(self):
        state = MetricsState()
        state.record_spawn(backend="cci", result="success")
        state.record_spawn(backend="cci", result="timeout")
        state.inc_active_sandboxes(1)
        state.inc_active_sandboxes(-1)
        state.observe_spawn_duration(backend="cci", seconds=1.5)
        state.record_cci_api_error(operation="create_pod", code="500")

        assert (
            state.registry.get_sample_value(
                "kimo_sandbox_spawn_total", {"backend": "cci", "result": "success"}
            )
            == 1.0
        )
        assert (
            state.registry.get_sample_value(
                "kimo_sandbox_spawn_total", {"backend": "cci", "result": "timeout"}
            )
            == 1.0
        )
        assert state.registry.get_sample_value("kimo_active_sandboxes", {}) == 0.0
        assert (
            state.registry.get_sample_value(
                "kimo_sandbox_spawn_duration_seconds_count", {"backend": "cci"}
            )
            == 1.0
        )
        assert (
            state.registry.get_sample_value(
                "kimo_cci_api_error_total", {"operation": "create_pod", "code": "500"}
            )
            == 1.0
        )

    def test_record_agent_load_failure_increments_reason_label(self):
        # hechun-fork-cci: kimo_agent_load_failure_total{reason} +1 per refusal.
        state = MetricsState()
        state.record_agent_load_failure(reason="agent_required_missing")
        state.record_agent_load_failure(reason="agent_required_missing")
        state.record_agent_load_failure(reason="subagent_unresolved")

        assert (
            state.registry.get_sample_value(
                "kimo_agent_load_failure_total", {"reason": "agent_required_missing"}
            )
            == 2.0
        )
        assert (
            state.registry.get_sample_value(
                "kimo_agent_load_failure_total", {"reason": "subagent_unresolved"}
            )
            == 1.0
        )

    def test_helpers_never_raise_on_bad_handle(self):
        # If a metric handle were somehow missing/broken, helpers must swallow.
        state = MetricsState()
        state.metrics = {}  # type: ignore[assignment]
        # none of these should raise despite the empty metrics dict.
        state.record_spawn(backend="cci", result="success")
        state.observe_spawn_duration(backend="cci", seconds=0.1)
        state.inc_active_sandboxes(1)
        state.record_cci_api_error(operation="x", code="y")
        state.record_agent_load_failure(reason="agent_required_missing")


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


class TestScrapeDerivesActiveGaugeFromListPods:
    """hechun-fork-cci Point 4: every /metrics scrape sets kimo_active_sandboxes
    from the REAL list_pods count — overwriting any stale (drifted) inc'd value,
    making the gauge a single source of truth that never漂移."""

    async def test_scrape_overwrites_stale_inc_with_real_pod_count(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("KIMI_METRICS_BASIC_AUTH_USER", raising=False)
        monkeypatch.delenv("KIMI_METRICS_BASIC_AUTH_PASSWORD", raising=False)

        state = MetricsState()

        class _Client:
            async def read_network(self, ns, name):
                return {"status": "Ready"}

            async def list_pods(self, ns, **kw):
                # 1 real sandbox Pod live; the gauge below is stale at 7.
                return [
                    {"metadata": {"name": "kimo-sandbox-live"}},
                    {"metadata": {"name": "cci-imagesnapshot-zzz"}},
                ]

        state.cci_client = _Client()  # type: ignore[assignment]
        # Simulate drift: a stale in-memory count left over from inc bookkeeping.
        state.metrics["active_sandboxes"].set(7)

        handler = _get_handler()
        app = _FakeApp()
        app.state.metrics = state
        resp = await handler(_FakeRequest(app, {}))
        assert resp.status_code == 200
        body = resp.body.decode("utf-8")
        # Scrape re-derived the gauge to the true count (1), discarding the stale 7.
        assert "kimo_active_sandboxes 1.0" in body
        assert "kimo_active_sandboxes 7.0" not in body
