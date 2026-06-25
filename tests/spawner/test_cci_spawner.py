"""Unit tests for CCISpawner: spawn/stop/healthcheck + Pod spec + backoff.

# hechun-fork-cci

Mock CciRestClient — no real CCI calls. asyncio.sleep is patched so the backoff
loop runs instantly.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from kimi_cli.web.spawner import SandboxHandle, SpawnTimeout
from kimi_cli.web.spawner.cci import CCISpawner
from kimi_cli.web.spawner.cci_client import CciApiError


class _FakeClient:
    def __init__(self):
        self.created: list = []
        self.deleted: list = []
        self.read_responses: list = []
        self.read_calls = 0
        self.read_raises: Exception | None = None

    async def create_pod(self, ns, pod):
        self.created.append((ns, pod))
        return {"metadata": {"name": pod["metadata"]["name"]}}

    async def read_pod(self, ns, name):
        self.read_calls += 1
        if self.read_raises is not None:
            raise self.read_raises
        return self.read_responses.pop(0)

    async def delete_pod(self, ns, name, grace=10):
        self.deleted.append((ns, name, grace))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch):
    import kimi_cli.web.spawner.cci as cci_mod

    async def _instant(_s):
        return None

    monkeypatch.setattr(cci_mod.asyncio, "sleep", _instant)


def _make_spawner(client: _FakeClient) -> CCISpawner:
    return CCISpawner(
        client=client,  # type: ignore[arg-type]
        token_provider=object(),  # type: ignore[arg-type]
        endpoint="cci.cn-x.myhuaweicloud.com",
        namespace="hechun-prod",
        region="cn-x",
        image="swr.cn-x.myhuaweicloud.com/hechun/kimo-sandbox:latest",
    )


class TestPodSpec:
    def test_pod_spec_matches_spec_section5(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("KIMI_WARM_MODE", raising=False)
        sp = _make_spawner(_FakeClient())
        sid = uuid4()
        pod = sp._build_pod_spec(sid, "hechun-7", {"KIMI_API_KEY": "k"})

        assert pod["apiVersion"] == "cci/v2"
        assert pod["kind"] == "Pod"
        assert pod["metadata"]["name"] == f"kimo-sandbox-{sid}"
        assert pod["metadata"]["labels"]["app"] == "kimo-sandbox"
        assert pod["metadata"]["labels"]["session_id"] == str(sid)
        # no yangtse.io/* annotations (network via namespace default Network);
        # image-snapshot annotation IS present by default (cold-start accel).
        _ann = pod["metadata"].get("annotations", {})
        assert _ann.get("cci.io/image-snapshot-create-if-not-present") == "true"
        assert not any(k.startswith("yangtse.io/") for k in _ann)

        c = pod["spec"]["containers"][0]
        assert c["stdin"] is True
        assert c["tty"] is False
        assert c["resources"]["requests"] == {"cpu": "2", "memory": "4Gi"}
        assert c["resources"]["limits"] == {"cpu": "2", "memory": "4Gi"}  # requests==limits
        assert c["env"] == [{"name": "KIMI_API_KEY", "value": "k"}]
        assert pod["spec"]["restartPolicy"] == "Never"
        assert pod["spec"]["terminationGracePeriodSeconds"] == 10
        # CCI auto-managed same-account pull secret (live-verified 2026-06-25;
        # swr-pull-secret would 401 — wrong name → anonymous pull).
        assert pod["spec"]["imagePullSecrets"] == [{"name": "imagepull-secret"}]

    def test_warm_mode_uses_warm_label(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("KIMI_WARM_MODE", "warm")
        sp = _make_spawner(_FakeClient())
        pod = sp._build_pod_spec(uuid4(), "owner", {})
        assert pod["metadata"]["labels"]["app"] == "kimo-sandbox-warm"


class TestSpawn:
    async def test_spawn_returns_handle_when_running(self):
        client = _FakeClient()
        client.read_responses = [{"status": {"phase": "Running", "podIP": "10.0.0.5"}}]
        sp = _make_spawner(client)
        sid = uuid4()
        handle = await sp.spawn(sid, "hechun-1", {})
        assert isinstance(handle, SandboxHandle)
        assert handle.backend == "cci"
        assert handle.handle_id == f"kimo-sandbox-{sid}"
        assert handle.network_endpoint == "ws://10.0.0.5:5494"
        assert handle.meta["pod_ip"] == "10.0.0.5"
        assert handle.created_at is not None

    async def test_spawn_polls_until_running(self):
        client = _FakeClient()
        client.read_responses = [
            {"status": {"phase": "Pending"}},
            {"status": {"phase": "Pending"}},
            {"status": {"phase": "Running", "podIP": "10.0.0.9"}},
        ]
        sp = _make_spawner(client)
        handle = await sp.spawn(uuid4(), "hechun-1", {})
        assert handle.network_endpoint == "ws://10.0.0.9:5494"
        assert client.read_calls == 3

    async def test_spawn_timeout_never_running(self, monkeypatch: pytest.MonkeyPatch):
        client = _FakeClient()
        # Always Pending → never resolves.
        client.read_responses = [{"status": {"phase": "Pending"}}] * 100
        sp = _make_spawner(client)
        with pytest.raises(SpawnTimeout):
            await sp.spawn(uuid4(), "hechun-1", {})

    async def test_spawn_timeout_when_running_without_ip(self):
        client = _FakeClient()
        client.read_responses = [{"status": {"phase": "Running"}}]  # no podIP
        sp = _make_spawner(client)
        with pytest.raises(SpawnTimeout):
            await sp.spawn(uuid4(), "hechun-1", {})


class TestStopHealthcheck:
    async def test_stop_calls_delete(self):
        client = _FakeClient()
        sp = _make_spawner(client)
        await sp.stop(SandboxHandle(backend="cci", handle_id="kimo-sandbox-x"))
        assert client.deleted == [("hechun-prod", "kimo-sandbox-x", 10)]

    async def test_stop_swallows_404(self):
        client = _FakeClient()

        async def delete_404(ns, name, grace=10):
            raise CciApiError("delete_pod", 404, "NotFound", "gone")

        client.delete_pod = delete_404  # type: ignore[assignment]
        sp = _make_spawner(client)
        # no raise
        await sp.stop(SandboxHandle(backend="cci", handle_id="x"))

    async def test_stop_reraises_non_404(self):
        client = _FakeClient()

        async def delete_500(ns, name, grace=10):
            raise CciApiError("delete_pod", 500, "Err", "boom")

        client.delete_pod = delete_500  # type: ignore[assignment]
        sp = _make_spawner(client)
        with pytest.raises(CciApiError):
            await sp.stop(SandboxHandle(backend="cci", handle_id="x"))

    async def test_healthcheck_true_when_running(self):
        client = _FakeClient()
        client.read_responses = [{"status": {"phase": "Running"}}]
        sp = _make_spawner(client)
        assert await sp.healthcheck(SandboxHandle(backend="cci", handle_id="x")) is True

    async def test_healthcheck_false_when_not_running(self):
        client = _FakeClient()
        client.read_responses = [{"status": {"phase": "Failed"}}]
        sp = _make_spawner(client)
        assert await sp.healthcheck(SandboxHandle(backend="cci", handle_id="x")) is False

    async def test_healthcheck_false_on_api_error(self):
        client = _FakeClient()
        client.read_raises = CciApiError("read_pod", 404, "NotFound", "gone")
        sp = _make_spawner(client)
        assert await sp.healthcheck(SandboxHandle(backend="cci", handle_id="x")) is False


def _metric_value(state, name: str, labels: dict[str, str] | None = None) -> float:
    """Read a single sample value out of a MetricsState registry (0.0 if absent)."""
    val = state.registry.get_sample_value(name, labels or {})
    return val if val is not None else 0.0


class TestSpawnMetrics:
    """spec §7.2 instrumentation: spawn_total / spawn_duration / active_sandboxes /
    cci_api_error_total are emitted on the real CCISpawner paths.
    """

    def _spawner_with_metrics(self, client: _FakeClient):
        prometheus = pytest.importorskip("prometheus_client")  # noqa: F841
        from kimi_cli.web.metrics import MetricsState

        sp = _make_spawner(client)
        state = MetricsState()
        sp.metrics = state  # also wires client.metrics via the property
        return sp, state

    async def test_spawn_success_emits_total_duration_and_active(self):
        client = _FakeClient()
        client.read_responses = [{"status": {"phase": "Running", "podIP": "10.0.0.5"}}]
        sp, state = self._spawner_with_metrics(client)

        await sp.spawn(uuid4(), "hechun-1", {})

        assert _metric_value(
            state, "kimo_sandbox_spawn_total", {"backend": "cci", "result": "success"}
        ) == 1.0
        assert _metric_value(state, "kimo_active_sandboxes") == 1.0
        # duration histogram observed exactly once on the success path.
        assert _metric_value(
            state, "kimo_sandbox_spawn_duration_seconds_count", {"backend": "cci"}
        ) == 1.0

    async def test_spawn_timeout_emits_result_timeout(self):
        client = _FakeClient()
        client.read_responses = [{"status": {"phase": "Pending"}}] * 100
        sp, state = self._spawner_with_metrics(client)

        with pytest.raises(SpawnTimeout):
            await sp.spawn(uuid4(), "hechun-1", {})

        assert _metric_value(
            state, "kimo_sandbox_spawn_total", {"backend": "cci", "result": "timeout"}
        ) == 1.0
        # no success, no active bump, no duration observation on timeout.
        assert _metric_value(
            state, "kimo_sandbox_spawn_total", {"backend": "cci", "result": "success"}
        ) == 0.0
        assert _metric_value(state, "kimo_active_sandboxes") == 0.0
        assert _metric_value(
            state, "kimo_sandbox_spawn_duration_seconds_count", {"backend": "cci"}
        ) == 0.0

    async def test_spawn_other_error_emits_result_failure_and_api_error(self):
        client = _FakeClient()

        async def create_500(ns, pod):
            raise CciApiError("create_pod", 500, "InternalError", "boom")

        client.create_pod = create_500  # type: ignore[assignment]
        sp, state = self._spawner_with_metrics(client)

        with pytest.raises(CciApiError):
            await sp.spawn(uuid4(), "hechun-1", {})

        # spawn classifies any non-timeout exception as result=failure.
        assert _metric_value(
            state, "kimo_sandbox_spawn_total", {"backend": "cci", "result": "failure"}
        ) == 1.0
        assert _metric_value(state, "kimo_active_sandboxes") == 0.0
        # NOTE: cci_api_error_total is emitted inside CciRestClient._parse (the
        # real raise point), not here — this fake raises the error directly,
        # bypassing _parse. The api-error counter is covered in the client tests.

    async def test_stop_decrements_active(self):
        client = _FakeClient()
        client.read_responses = [{"status": {"phase": "Running", "podIP": "10.0.0.5"}}]
        sp, state = self._spawner_with_metrics(client)

        handle = await sp.spawn(uuid4(), "hechun-1", {})
        assert _metric_value(state, "kimo_active_sandboxes") == 1.0
        await sp.stop(handle)
        assert _metric_value(state, "kimo_active_sandboxes") == 0.0

    async def test_stop_404_still_decrements_active(self):
        client = _FakeClient()
        client.read_responses = [{"status": {"phase": "Running", "podIP": "10.0.0.5"}}]
        sp, state = self._spawner_with_metrics(client)
        handle = await sp.spawn(uuid4(), "hechun-1", {})

        async def delete_404(ns, name, grace=10):
            raise CciApiError("delete_pod", 404, "NotFound", "gone")

        client.delete_pod = delete_404  # type: ignore[assignment]
        await sp.stop(handle)  # no raise
        assert _metric_value(state, "kimo_active_sandboxes") == 0.0

    async def test_stop_non_404_does_not_decrement(self):
        client = _FakeClient()
        client.read_responses = [{"status": {"phase": "Running", "podIP": "10.0.0.5"}}]
        sp, state = self._spawner_with_metrics(client)
        handle = await sp.spawn(uuid4(), "hechun-1", {})

        async def delete_500(ns, name, grace=10):
            raise CciApiError("delete_pod", 500, "Err", "boom")

        client.delete_pod = delete_500  # type: ignore[assignment]
        with pytest.raises(CciApiError):
            await sp.stop(handle)
        # delete failed → Pod may still be Running → gauge stays at 1.
        assert _metric_value(state, "kimo_active_sandboxes") == 1.0

    async def test_metrics_none_does_not_crash(self):
        # No metrics wired (docker path / KIMI_METRICS_ENABLED off): spawn/stop
        # must behave exactly as before and never touch a None handle.
        client = _FakeClient()
        client.read_responses = [{"status": {"phase": "Running", "podIP": "10.0.0.5"}}]
        sp = _make_spawner(client)
        assert sp.metrics is None
        handle = await sp.spawn(uuid4(), "hechun-1", {})
        await sp.stop(handle)  # no raise


class TestSpawnOrphanSelfHeal:
    """A. spawn 撞名自愈: create_pod 遇 409 → 删旧 Pod → 等其消失 → 重建一次。"""

    async def test_409_deletes_old_then_recreates(self):
        client = _FakeClient()
        # First create_pod → 409 (orphan name clash); second → success.
        create_calls = {"n": 0}
        orig_create = client.create_pod

        async def create_409_then_ok(ns, pod):
            create_calls["n"] += 1
            if create_calls["n"] == 1:
                raise CciApiError("create_pod", 409, "AlreadyExists", "exists")
            return await orig_create(ns, pod)

        client.create_pod = create_409_then_ok  # type: ignore[assignment]
        # read_pod: first call (wait_pod_gone) → 404 gone; then the Running poll.
        client.read_raises = None
        reads = {"n": 0}

        async def read_seq(ns, name):
            reads["n"] += 1
            if reads["n"] == 1:
                # _wait_pod_gone polls read_pod → 404 = gone.
                raise CciApiError("read_pod", 404, "NotFound", "gone")
            return {"status": {"phase": "Running", "podIP": "10.0.0.7"}}

        client.read_pod = read_seq  # type: ignore[assignment]
        sp = _make_spawner(client)
        sid = uuid4()
        handle = await sp.spawn(sid, "hechun-1", {})

        assert create_calls["n"] == 2  # created twice (after delete)
        assert handle.network_endpoint == "ws://10.0.0.7:5494"
        # old orphan Pod was deleted with grace=0.
        assert client.deleted
        assert client.deleted[0] == ("hechun-prod", f"kimo-sandbox-{sid}", 0)

    async def test_non_409_create_error_propagates(self):
        client = _FakeClient()

        async def create_500(ns, pod):
            raise CciApiError("create_pod", 500, "InternalError", "boom")

        client.create_pod = create_500  # type: ignore[assignment]
        sp = _make_spawner(client)
        with pytest.raises(CciApiError) as ei:
            await sp.spawn(uuid4(), "hechun-1", {})
        assert ei.value.status == 500
        # No delete attempted for a non-409 failure.
        assert client.deleted == []


class TestReconcileOrphans:
    """B. 启动孤儿清扫: 只删 kimo-sandbox-* 前缀，不碰 cci-imagesnapshot-*/其它。"""

    def _client_with_pods(self, names: list[str]) -> _FakeClient:
        client = _FakeClient()

        async def list_pods(ns, label_selector=None):
            return [{"metadata": {"name": n}} for n in names]

        client.list_pods = list_pods  # type: ignore[attr-defined]
        return client

    async def test_only_deletes_sandbox_prefix(self):
        client = self._client_with_pods(
            [
                "kimo-sandbox-aaa",
                "kimo-sandbox-bbb",
                "cci-imagesnapshot-xyz",  # CCI 托管快照 Pod — 绝不删
                "some-other-workload",  # 别的工作负载 — 绝不删
            ]
        )
        sp = _make_spawner(client)
        n = await sp.reconcile_orphans()
        assert n == 2
        deleted_names = {d[1] for d in client.deleted}
        assert deleted_names == {"kimo-sandbox-aaa", "kimo-sandbox-bbb"}
        # grace=0 on every delete.
        assert all(d[2] == 0 for d in client.deleted)

    async def test_no_sandbox_pods_deletes_nothing(self):
        client = self._client_with_pods(["cci-imagesnapshot-1", "redis-0"])
        sp = _make_spawner(client)
        assert await sp.reconcile_orphans() == 0
        assert client.deleted == []

    async def test_per_delete_failure_is_best_effort(self):
        client = self._client_with_pods(["kimo-sandbox-a", "kimo-sandbox-b"])

        async def delete_first_500(ns, name, grace=10):
            if name == "kimo-sandbox-a":
                raise CciApiError("delete_pod", 500, "Err", "boom")
            client.deleted.append((ns, name, grace))

        client.delete_pod = delete_first_500  # type: ignore[assignment]
        sp = _make_spawner(client)
        # One failure logged + skipped; the other still deleted; no raise.
        n = await sp.reconcile_orphans()
        assert n == 1
        assert client.deleted == [("hechun-prod", "kimo-sandbox-b", 0)]

    async def test_list_pods_failure_returns_zero(self):
        client = _FakeClient()

        async def list_boom(ns, label_selector=None):
            raise RuntimeError("network down")

        client.list_pods = list_boom  # type: ignore[assignment]
        sp = _make_spawner(client)
        # Best-effort: list failure → 0, no raise.
        assert await sp.reconcile_orphans() == 0


class TestFromEnv:
    def test_from_env_requires_keys(self, monkeypatch: pytest.MonkeyPatch):
        for k in ("HUAWEICLOUD_AK", "HUAWEICLOUD_SK", "HUAWEICLOUD_CCI_REGION"):
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(RuntimeError):
            CCISpawner.from_env()

    def test_from_env_builds_endpoint(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HUAWEICLOUD_AK", "ak")
        monkeypatch.setenv("HUAWEICLOUD_SK", "sk")
        monkeypatch.setenv("HUAWEICLOUD_CCI_REGION", "cn-north-4")
        monkeypatch.delenv("HUAWEICLOUD_CCI_ENDPOINT", raising=False)
        # Pin the namespace explicitly so the test is hermetic regardless of any
        # HUAWEICLOUD_CCI_NAMESPACE inherited from the process env / deploy config.
        monkeypatch.setenv("HUAWEICLOUD_CCI_NAMESPACE", "hechun-prod")
        sp = CCISpawner.from_env()
        assert sp.endpoint == "cci.cn-north-4.myhuaweicloud.com"
        assert sp.namespace == "hechun-prod"
