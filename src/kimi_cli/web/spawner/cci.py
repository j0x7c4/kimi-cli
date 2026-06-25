"""CCISpawner —华为云 CCI 2.0 sandbox spawner (spec §5/§6).

# hechun-fork-cci

Assembles the auth (C-1), REST client (C-2) and exec stream (C-3) into the
:class:`~kimi_cli.web.spawner.SandboxSpawner` Protocol:

    spawn        POST .../pods → poll GET .../pods/{name} until Running (§6 backoff)
    attach       GET  .../pods/{name}/exec WebSocket (IAM token handshake)
    stop         DELETE .../pods/{name}?gracePeriodSeconds=10  (计费止于 delete)
    healthcheck  GET  .../pods/{name} → status.phase == "Running"

Pod spec is the real cci/v2 shape (spec §5): ``apiVersion: cci/v2``,
requests==limits 2C/4Gi (CCI 强制相等), ``stdin: true`` (warm BIND / attach 前提),
``restartPolicy: Never``, **no ``yangtse.io/*`` annotations** (网络由 namespace
default Network 决定, §4).
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import UTC, datetime
from uuid import UUID

from kimi_cli.web.spawner import SandboxHandle, SpawnTimeout
from kimi_cli.web.spawner.cci_auth import HuaweiSigner, TokenProvider
from kimi_cli.web.spawner.cci_client import CciRestClient
from kimi_cli.web.spawner.cci_exec import KimoExecStream

# Spawn poll backoff (spec §6): 2/4/8s, cap 8s, total timeout 60s.
_SPAWN_TIMEOUT_S = 60
_BACKOFF_START_S = 2
_BACKOFF_CAP_S = 8

# Worker gateway port the sandbox listens on (matches web/app.py DEFAULT_PORT).
_WORKER_PORT = 5494

# Pod resource spec — CCI 2.0 forces requests == limits (spec §5).
_CPU = "2"
_MEMORY = "4Gi"
_GRACE_S = 10

# Pod 主进程 = keepalive；worker 经 exec 起（cci_process attach → /start-sandbox.sh）。
# 镜像 CMD 是 /start-sandbox.sh（worker 读主进程 stdin），但 CCI 不喂主 stdin → worker 读 EOF
# 即退出 → Pod Failed（2026-06-25 整链实测）。改主进程 keepalive，worker 由 gateway 经 exec 驱动。
_KEEPALIVE_CMD = ["sleep", "infinity"]


def _label_safe(value: str, fallback: str = "x") -> str:
    """Coerce to a valid k8s label value (CCI admission 强校验，2026-06-25 实测).

    规则：仅 ``[A-Za-z0-9._-]``、首尾必须字母数字、≤63。``__anonymous__`` 这类首尾下划线
    会被 CCI 拒（HTTP 422）。非法字符→``-``，首尾非字母数字剥除，空则回退 ``fallback``。
    """
    v = re.sub(r"[^A-Za-z0-9._-]", "-", value or "")[:63]
    v = v.strip("._-")
    return v or fallback


class CCISpawner:
    """SandboxSpawner backed by华为云 CCI 2.0 (spec §5/§6)."""

    def __init__(
        self,
        *,
        client: CciRestClient,
        token_provider: TokenProvider,
        endpoint: str,
        namespace: str,
        region: str,
        image: str,
        # CCI 2.0 auto-manages a namespace-scoped ``imagepull-secret`` (rolling
        # 24h token) for same-account SWR pulls — live-verified the blessed path
        # (2026-06-25). Referencing a non-existent ``swr-pull-secret`` makes
        # containerd fall back to anonymous → 401 on private repos.
        image_pull_secret: str | None = "imagepull-secret",
    ) -> None:
        self.client = client
        self.token_provider = token_provider
        self.endpoint = endpoint
        self.namespace = namespace
        self.region = region
        self.image = image
        self.image_pull_secret = image_pull_secret

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> CCISpawner:
        """Build from ``HUAWEICLOUD_*`` env (spec §11 secrets keys)."""
        ak = os.environ.get("HUAWEICLOUD_AK", "")
        sk = os.environ.get("HUAWEICLOUD_SK", "")
        region = os.environ.get("HUAWEICLOUD_CCI_REGION", "")
        namespace = os.environ.get("HUAWEICLOUD_CCI_NAMESPACE", "kalinin-test")
        if not (ak and sk and region):
            raise RuntimeError(
                "KIMI_SPAWNER_BACKEND=cci requires HUAWEICLOUD_AK / _SK / _CCI_REGION"
            )
        endpoint = os.environ.get(
            "HUAWEICLOUD_CCI_ENDPOINT", f"cci.{region}.myhuaweicloud.com"
        )
        image = os.environ.get(
            "SANDBOX_IMAGE",
            f"swr.{region}.myhuaweicloud.com/hechunmedical/kimo-sandbox:latest",
        )
        signer = HuaweiSigner(ak, sk)
        client = CciRestClient(endpoint, signer)
        token_provider = TokenProvider(ak, sk, region)
        return cls(
            client=client,
            token_provider=token_provider,
            endpoint=endpoint,
            namespace=namespace,
            region=region,
            image=image,
        )

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def spawn(self, sid: UUID, owner_id: str, env: dict[str, str]) -> SandboxHandle:
        pod = self._build_pod_spec(sid, owner_id, env)
        resp = await self.client.create_pod(self.namespace, pod)
        name = resp.get("metadata", {}).get("name") or pod["metadata"]["name"]
        pod_ip = await self._wait_running(name, timeout=_SPAWN_TIMEOUT_S)
        return SandboxHandle(
            backend="cci",
            handle_id=name,
            network_endpoint=f"ws://{pod_ip}:{_WORKER_PORT}",
            created_at=self._now(),
            meta={"pod_ip": pod_ip, "namespace": self.namespace},
        )

    async def attach(self, handle: SandboxHandle) -> KimoExecStream:
        stream = KimoExecStream()
        await stream.connect(
            self.endpoint, self.namespace, handle.handle_id, self.token_provider
        )
        return stream

    async def stop(self, handle: SandboxHandle) -> None:
        # 计费止于 delete 返回 (spec §6) — best-effort; already-gone is fine.
        from kimi_cli.web.spawner.cci_client import CciApiError  # noqa: PLC0415

        try:
            await self.client.delete_pod(self.namespace, handle.handle_id, grace=_GRACE_S)
        except CciApiError as e:
            if e.status == 404:
                return
            raise

    async def healthcheck(self, handle: SandboxHandle) -> bool:
        from kimi_cli.web.spawner.cci_client import CciApiError  # noqa: PLC0415

        try:
            pod = await self.client.read_pod(self.namespace, handle.handle_id)
        except CciApiError:
            return False
        return pod.get("status", {}).get("phase") == "Running"

    # ── internals ─────────────────────────────────────────────────────────────

    def _build_pod_spec(self, sid: UUID, owner_id: str, env: dict[str, str]) -> dict:
        warm = (os.environ.get("KIMI_WARM_MODE") or "").strip().lower() in {"1", "true", "warm"}
        app_label = "kimo-sandbox-warm" if warm else "kimo-sandbox"
        pod = {
            "apiVersion": "cci/v2",
            "kind": "Pod",
            "metadata": {
                "name": f"kimo-sandbox-{sid}",
                "namespace": self.namespace,
                "labels": {
                    "app": app_label,
                    "owner": _label_safe(owner_id, "anon"),
                    "session_id": _label_safe(str(sid)),
                },
                # 无 yangtse.io/* 注解: 网络由 namespace default Network 决定 (§4).
            },
            "spec": {
                "containers": [
                    {
                        "name": "sandbox",
                        "image": self.image,
                        "imagePullPolicy": "Always",
                        # 主进程 keepalive；worker 经 exec 起（见 _KEEPALIVE_CMD 注释）。
                        "command": _KEEPALIVE_CMD,
                        "stdin": True,  # ★ warm BIND / attach 必需
                        "tty": False,
                        "resources": {
                            "requests": {"cpu": _CPU, "memory": _MEMORY},
                            "limits": {"cpu": _CPU, "memory": _MEMORY},  # CCI 强制 requests=limits
                        },
                        "env": [{"name": k, "value": v} for k, v in env.items()],
                    }
                ],
                "restartPolicy": "Never",
                "terminationGracePeriodSeconds": _GRACE_S,
            },
        }
        if self.image_pull_secret:
            pod["spec"]["imagePullSecrets"] = [{"name": self.image_pull_secret}]
        return pod

    async def _wait_running(self, name: str, timeout: int) -> str:
        """Poll GET .../pods/{name} until Running; backoff 2/4/8s cap 60s (§6)."""
        delay, waited = _BACKOFF_START_S, 0
        while waited < timeout:
            pod = await self.client.read_pod(self.namespace, name)
            status = pod.get("status", {})
            if status.get("phase") == "Running":
                pod_ip = status.get("podIP")
                if not pod_ip:
                    raise SpawnTimeout(name)
                return pod_ip
            await asyncio.sleep(delay)
            waited += delay
            delay = min(delay * 2, _BACKOFF_CAP_S)
        raise SpawnTimeout(name)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(tz=UTC)


__all__ = ["CCISpawner"]
