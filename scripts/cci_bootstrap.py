#!/usr/bin/env python3
"""M0 one-shot: idempotently create the CCI namespace + default Network (spec §4.1).

# hechun-fork-cci

Run once per environment (manually or as part of M0 provisioning). Creates:

  1. Namespace ``kalinin-test``      POST /apis/cci/v2/namespaces
  2. default Network bound to the    POST /apis/yangtse/v2/namespaces/{ns}/networks
     sandbox-subnet

Both are idempotent: an already-existing resource (HTTP 409 Conflict) is treated
as success. Network creation needs the sandbox subnet + security group ids from
env (spec §4.1):

    HUAWEICLOUD_AK / HUAWEICLOUD_SK / HUAWEICLOUD_CCI_REGION
    HUAWEICLOUD_CCI_NAMESPACE      (default: kalinin-test; test/prod 各自独立 ns)
    HUAWEICLOUD_SANDBOX_SUBNET_ID
    HUAWEICLOUD_SANDBOX_SG_ID
    HUAWEICLOUD_CCI_NETWORK        (default: default-network)

``--dry-run`` prints the resources it would POST without calling华为云 (used by
the unit test and for review).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

# Make the package importable when run as a plain script.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

DEFAULT_NAMESPACE = "kalinin-test"
DEFAULT_NETWORK = "default-network"


def build_namespace(name: str) -> dict:
    return {"apiVersion": "cci/v2", "kind": "Namespace", "metadata": {"name": name}}


def build_network(
    name: str,
    namespace: str,
    subnet_id: str,
    sg_id: str,
    domain_id: str = "",
    project_id: str = "",
) -> dict:
    """Default Network spec (spec §4.1 Step 2)."""
    metadata: dict = {"name": name, "namespace": namespace}
    # 部分 region/版本建 Network 要求带 domain-id/project-id 注解；提供了才加（控制台"我的凭证"查）
    annotations = {}
    if domain_id:
        annotations["yangtse.io/domain-id"] = domain_id
    if project_id:
        annotations["yangtse.io/project-id"] = project_id
    if annotations:
        metadata["annotations"] = annotations
    return {
        "apiVersion": "yangtse/v2",
        "kind": "Network",
        "metadata": metadata,
        "spec": {
            "networkType": "underlay_neutron",
            "subnets": [{"subnetID": subnet_id}],
            "securityGroups": [sg_id],
            "ipFamilies": ["IPv4"],
            "defaultNetwork": True,
        },
    }


async def _ensure(coro, what: str) -> None:
    """Await a create coro; treat 409 Conflict as already-exists (idempotent)."""
    from kimi_cli.web.spawner.cci_client import CciApiError  # noqa: PLC0415

    try:
        await coro
        print(f"[cci_bootstrap] created {what}")
    except CciApiError as e:
        if e.status == 409:
            print(f"[cci_bootstrap] {what} already exists (409), ok")
            return
        # CCI 2.0 控制台建 namespace 时会自动建 <ns>-default-network 绑默认子网；
        # 再建绑同子网的 network 被 webhook 拒为 "subnetID ... not allowed to be
        # duplicated" → 视为已存在（M0 live 实测）。
        if e.status == 403 and "duplicat" in str(e).lower():
            print(f"[cci_bootstrap] {what}: subnet 已被现有 network 绑定 (403 duplicate)，ok")
            return
        raise


async def bootstrap(*, dry_run: bool) -> int:
    namespace = os.environ.get("HUAWEICLOUD_CCI_NAMESPACE", DEFAULT_NAMESPACE)
    network = os.environ.get("HUAWEICLOUD_CCI_NETWORK", DEFAULT_NETWORK)
    subnet_id = os.environ.get("HUAWEICLOUD_SANDBOX_SUBNET_ID", "")
    sg_id = os.environ.get("HUAWEICLOUD_SANDBOX_SG_ID", "")
    domain_id = os.environ.get("HUAWEICLOUD_DOMAIN_ID", "")
    project_id = os.environ.get("HUAWEICLOUD_PROJECT_ID", "")

    ns_body = build_namespace(namespace)
    net_body = build_network(network, namespace, subnet_id, sg_id, domain_id, project_id)

    if dry_run:
        print("=== DRY RUN — would POST ===")
        print("Namespace:", json.dumps(ns_body, ensure_ascii=False, indent=2))
        print("Network:", json.dumps(net_body, ensure_ascii=False, indent=2))
        return 0

    ak = os.environ.get("HUAWEICLOUD_AK", "")
    sk = os.environ.get("HUAWEICLOUD_SK", "")
    region = os.environ.get("HUAWEICLOUD_CCI_REGION", "")
    if not (ak and sk and region):
        print("ERROR: HUAWEICLOUD_AK / _SK / _CCI_REGION required", file=sys.stderr)
        return 2
    if not (subnet_id and sg_id):
        print(
            "ERROR: HUAWEICLOUD_SANDBOX_SUBNET_ID / _SANDBOX_SG_ID required",
            file=sys.stderr,
        )
        return 2

    from kimi_cli.web.spawner.cci_auth import HuaweiSigner  # noqa: PLC0415
    from kimi_cli.web.spawner.cci_client import CciRestClient  # noqa: PLC0415

    endpoint = os.environ.get(
        "HUAWEICLOUD_CCI_ENDPOINT", f"cci.{region}.myhuaweicloud.com"
    )
    client = CciRestClient(endpoint, HuaweiSigner(ak, sk))

    await _ensure(client.create_namespace(ns_body), f"namespace {namespace}")
    await _ensure(
        client.create_network(namespace, net_body), f"network {network}"
    )
    print("[cci_bootstrap] done. Verify Network.status == Ready in the console.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Idempotent CCI namespace + Network bootstrap")
    parser.add_argument("--dry-run", action="store_true", help="print resources, do not call CCI")
    args = parser.parse_args()
    return asyncio.run(bootstrap(dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
