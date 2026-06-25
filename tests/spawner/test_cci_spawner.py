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
        # no yangtse.io/* annotations
        assert "annotations" not in pod["metadata"]

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
