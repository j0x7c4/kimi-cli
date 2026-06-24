"""Unit tests for CciRestClient: URI / method correctness (mock httpx).

# hechun-fork-cci

Verifies the real CCI 2.0 paths (spec §1.1): pods under /apis/cci/v2,
networks under /apis/yangtse/v2. httpx.AsyncClient.request is mocked so no
network IO happens.
"""

from __future__ import annotations

import json

import pytest

from kimi_cli.web.spawner.cci_client import CciApiError, CciRestClient


class _FakeSigner:
    def sign(self, method, url, headers, body):
        h = dict(headers)
        h["Authorization"] = "SDK-HMAC-SHA256 fake"
        return h


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str | None = None):
        self.status_code = status_code
        self.text = text if text is not None else (json.dumps(payload) if payload else "")


class _FakeAsyncClient:
    """Records the single request made; returns a queued response."""

    captured: dict = {}
    response: _FakeResponse = _FakeResponse(200, {})

    def __init__(self, *_a, **_kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def request(self, method, url, content=None, headers=None):
        _FakeAsyncClient.captured = {
            "method": method,
            "url": url,
            "content": content,
            "headers": headers,
        }
        return _FakeAsyncClient.response


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> CciRestClient:
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    _FakeAsyncClient.captured = {}
    _FakeAsyncClient.response = _FakeResponse(200, {})
    return CciRestClient("cci.cn-x.myhuaweicloud.com", _FakeSigner())


class TestUris:
    async def test_create_pod_uri(self, client: CciRestClient):
        _FakeAsyncClient.response = _FakeResponse(201, {"metadata": {"name": "p1"}})
        out = await client.create_pod("hechun-prod", {"kind": "Pod"})
        cap = _FakeAsyncClient.captured
        assert cap["method"] == "POST"
        assert cap["url"] == (
            "https://cci.cn-x.myhuaweicloud.com/apis/cci/v2/namespaces/hechun-prod/pods"
        )
        assert cap["headers"]["Authorization"].startswith("SDK-HMAC-SHA256")
        assert out["metadata"]["name"] == "p1"

    async def test_read_pod_uri(self, client: CciRestClient):
        _FakeAsyncClient.response = _FakeResponse(200, {"status": {"phase": "Running"}})
        await client.read_pod("hechun-prod", "kimo-sandbox-x")
        cap = _FakeAsyncClient.captured
        assert cap["method"] == "GET"
        assert cap["url"].endswith("/apis/cci/v2/namespaces/hechun-prod/pods/kimo-sandbox-x")

    async def test_delete_pod_includes_grace(self, client: CciRestClient):
        _FakeAsyncClient.response = _FakeResponse(200, {})
        await client.delete_pod("ns", "p", grace=10)
        cap = _FakeAsyncClient.captured
        assert cap["method"] == "DELETE"
        assert cap["url"].endswith("/apis/cci/v2/namespaces/ns/pods/p?gracePeriodSeconds=10")

    async def test_list_pods_label_selector(self, client: CciRestClient):
        _FakeAsyncClient.response = _FakeResponse(200, {"items": [{"a": 1}]})
        items = await client.list_pods("ns", "app=kimo-sandbox-warm")
        cap = _FakeAsyncClient.captured
        assert "/apis/cci/v2/namespaces/ns/pods?labelSelector=" in cap["url"]
        assert "app%3Dkimo-sandbox-warm" in cap["url"]
        assert items == [{"a": 1}]

    async def test_read_network_uses_yangtse_group(self, client: CciRestClient):
        _FakeAsyncClient.response = _FakeResponse(200, {"status": "Ready"})
        await client.read_network("hechun-prod", "default-network")
        cap = _FakeAsyncClient.captured
        assert cap["method"] == "GET"
        assert cap["url"].endswith(
            "/apis/yangtse/v2/namespaces/hechun-prod/networks/default-network"
        )

    async def test_create_namespace_uri(self, client: CciRestClient):
        _FakeAsyncClient.response = _FakeResponse(201, {})
        await client.create_namespace({"kind": "Namespace"})
        cap = _FakeAsyncClient.captured
        assert cap["url"].endswith("/apis/cci/v2/namespaces")

    async def test_create_network_uri(self, client: CciRestClient):
        _FakeAsyncClient.response = _FakeResponse(201, {})
        await client.create_network("ns", {"kind": "Network"})
        cap = _FakeAsyncClient.captured
        assert cap["url"].endswith("/apis/yangtse/v2/namespaces/ns/networks")


class TestErrorHandling:
    async def test_4xx_raises_cci_api_error_with_code(self, client: CciRestClient):
        _FakeAsyncClient.response = _FakeResponse(
            409, {"code": "Conflict", "message": "already exists"}
        )
        with pytest.raises(CciApiError) as ei:
            await client.create_namespace({"kind": "Namespace"})
        assert ei.value.status == 409
        assert ei.value.code == "Conflict"
        assert ei.value.operation == "create_namespace"

    async def test_5xx_raises(self, client: CciRestClient):
        _FakeAsyncClient.response = _FakeResponse(500, text="boom")
        with pytest.raises(CciApiError) as ei:
            await client.read_pod("ns", "p")
        assert ei.value.status == 500
