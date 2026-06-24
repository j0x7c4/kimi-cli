"""Unit tests for cci_auth: HuaweiSigner + TokenProvider (mock SDK / IAM).

# hechun-fork-cci
"""

from __future__ import annotations

import sys
import types
from datetime import timedelta

import pytest

from kimi_cli.web.spawner.cci_auth import HuaweiSigner, TokenProvider


def _install_fake_hwsdk(monkeypatch: pytest.MonkeyPatch, recorder: dict) -> None:
    """Install a fake huaweicloudsdkcore that records the signed request."""

    cred_mod = types.ModuleType("huaweicloudsdkcore.auth.credentials")
    signer_mod = types.ModuleType("huaweicloudsdkcore.signer.signer")
    sdk_request_mod = types.ModuleType("huaweicloudsdkcore.sdk_request")

    class BasicCredentials:
        def __init__(self, ak, sk):
            recorder["ak"] = ak
            recorder["sk"] = sk

    class SdkRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.header_params = dict(kwargs.get("header_params", {}))

    class Signer:
        def __init__(self, credentials):
            self._cred = credentials

        def sign(self, sdk_request):
            recorder["signed_method"] = sdk_request.method
            recorder["signed_uri"] = sdk_request.uri
            sdk_request.header_params["Authorization"] = "SDK-HMAC-SHA256 fake"
            sdk_request.header_params["X-Sdk-Date"] = "20260624T000000Z"

    cred_mod.BasicCredentials = BasicCredentials
    signer_mod.Signer = Signer
    sdk_request_mod.SdkRequest = SdkRequest

    # Parent packages must exist for ``from x.y.z import w``.
    for name, mod in {
        "huaweicloudsdkcore": types.ModuleType("huaweicloudsdkcore"),
        "huaweicloudsdkcore.auth": types.ModuleType("huaweicloudsdkcore.auth"),
        "huaweicloudsdkcore.auth.credentials": cred_mod,
        "huaweicloudsdkcore.signer": types.ModuleType("huaweicloudsdkcore.signer"),
        "huaweicloudsdkcore.signer.signer": signer_mod,
        "huaweicloudsdkcore.sdk_request": sdk_request_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)


class TestHuaweiSigner:
    def test_rejects_empty_keys(self):
        with pytest.raises(ValueError):
            HuaweiSigner("", "sk")

    def test_sign_adds_auth_headers(self, monkeypatch: pytest.MonkeyPatch):
        rec: dict = {}
        _install_fake_hwsdk(monkeypatch, rec)
        signer = HuaweiSigner("AK123", "SK456")
        headers = signer.sign(
            "POST",
            "https://cci.cn-x.myhuaweicloud.com/apis/cci/v2/namespaces",
            {"Content-Type": "application/json"},
            b'{"x":1}',
        )
        assert headers["Authorization"].startswith("SDK-HMAC-SHA256")
        assert "X-Sdk-Date" in headers
        # original header preserved
        assert headers["Content-Type"] == "application/json"
        assert rec["ak"] == "AK123"
        assert rec["signed_method"] == "POST"

    def test_sign_does_not_mutate_input(self, monkeypatch: pytest.MonkeyPatch):
        rec: dict = {}
        _install_fake_hwsdk(monkeypatch, rec)
        signer = HuaweiSigner("AK", "SK")
        original = {"Content-Type": "application/json"}
        signer.sign("GET", "https://h/x", original, b"")
        assert "Authorization" not in original  # input untouched


class TestTokenProvider:
    def test_requires_region(self):
        with pytest.raises(ValueError):
            TokenProvider("ak", "sk", "")

    async def test_token_cached_until_expiry(self, monkeypatch: pytest.MonkeyPatch):
        tp = TokenProvider("ak", "sk", "cn-x")
        calls = {"n": 0}

        async def fake_fetch():
            calls["n"] += 1
            return f"token-{calls['n']}"

        monkeypatch.setattr(tp, "_fetch_token", fake_fetch)

        t1 = await tp.token()
        t2 = await tp.token()
        assert t1 == t2 == "token-1"
        assert calls["n"] == 1  # cache hit, only one fetch

    async def test_token_refreshes_after_expiry(self, monkeypatch: pytest.MonkeyPatch):
        tp = TokenProvider("ak", "sk", "cn-x")
        calls = {"n": 0}

        async def fake_fetch():
            calls["n"] += 1
            return f"token-{calls['n']}"

        monkeypatch.setattr(tp, "_fetch_token", fake_fetch)

        await tp.token()
        # Force the cached token to look expired.
        assert tp._expires_at is not None
        tp._expires_at = tp._now() - timedelta(seconds=1)
        t2 = await tp.token()
        assert t2 == "token-2"
        assert calls["n"] == 2

    async def test_invalidate_forces_refetch(self, monkeypatch: pytest.MonkeyPatch):
        tp = TokenProvider("ak", "sk", "cn-x")
        calls = {"n": 0}

        async def fake_fetch():
            calls["n"] += 1
            return f"token-{calls['n']}"

        monkeypatch.setattr(tp, "_fetch_token", fake_fetch)
        await tp.token()
        tp.invalidate()
        await tp.token()
        assert calls["n"] == 2
