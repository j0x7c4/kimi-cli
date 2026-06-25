"""Unit tests for scripts/cci_bootstrap.py: namespace/network spec + idempotency.

# hechun-fork-cci
"""

from __future__ import annotations

import importlib.util
import os

import pytest

_SPEC_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "cci_bootstrap.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("cci_bootstrap", _SPEC_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestResourceSpecs:
    def test_namespace_spec(self):
        mod = _load_module()
        ns = mod.build_namespace("hechun-prod")
        assert ns == {
            "apiVersion": "cci/v2",
            "kind": "Namespace",
            "metadata": {"name": "hechun-prod"},
        }

    def test_network_spec_section_4_1(self):
        mod = _load_module()
        net = mod.build_network("default-network", "hechun-prod", "subnet-123", "sg-456")
        assert net["apiVersion"] == "yangtse/v2"
        assert net["kind"] == "Network"
        assert net["metadata"] == {"name": "default-network", "namespace": "hechun-prod"}
        spec = net["spec"]
        # underscore, not hyphen: admission webhook validate.yangtse.cni rejects
        # "underlay-neutron" (live-verified 2026-06-24).
        assert spec["networkType"] == "underlay_neutron"
        assert spec["subnets"] == [{"subnetID": "subnet-123"}]
        assert spec["securityGroups"] == ["sg-456"]
        assert spec["ipFamilies"] == ["IPv4"]
        assert spec["defaultNetwork"] is True


class TestIdempotency:
    async def test_ensure_treats_409_as_ok(self):
        mod = _load_module()
        from kimi_cli.web.spawner.cci_client import CciApiError

        async def conflict():
            raise CciApiError("create_namespace", 409, "Conflict", "exists")

        # no raise
        await mod._ensure(conflict(), "namespace x")

    async def test_ensure_reraises_other_errors(self):
        mod = _load_module()
        from kimi_cli.web.spawner.cci_client import CciApiError

        async def server_err():
            raise CciApiError("create_namespace", 500, "Err", "boom")

        with pytest.raises(CciApiError):
            await mod._ensure(server_err(), "namespace x")


class TestDryRun:
    async def test_dry_run_returns_zero_without_calling_cloud(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        mod = _load_module()
        monkeypatch.setenv("HUAWEICLOUD_CCI_NAMESPACE", "hechun-prod")
        monkeypatch.setenv("HUAWEICLOUD_SANDBOX_SUBNET_ID", "subnet-1")
        monkeypatch.setenv("HUAWEICLOUD_SANDBOX_SG_ID", "sg-1")
        rc = await mod.bootstrap(dry_run=True)
        assert rc == 0
