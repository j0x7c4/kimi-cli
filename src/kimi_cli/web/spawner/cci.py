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
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from kimi_cli.web.spawner import SandboxHandle, SpawnTimeout
from kimi_cli.web.spawner.cci_auth import HuaweiSigner, TokenProvider
from kimi_cli.web.spawner.cci_client import CciRestClient
from kimi_cli.web.spawner.cci_exec import KimoExecStream

if TYPE_CHECKING:
    from kimi_cli.web.metrics import MetricsState

# Stable label values for the spawn metrics (spec §7.2). Kept as constants so the
# Grafana PromQL and these emit points can never drift.
_BACKEND = "cci"
_RESULT_SUCCESS = "success"
_RESULT_TIMEOUT = "timeout"
_RESULT_FAILURE = "failure"

# Spawn poll backoff (spec §6): 2/4/8s, cap 8s, total timeout 60s.
# 默认 180s：CCI 冷启动拉 4.1GB sandbox 镜像实测 ~70s（>旧 60s → 整链 spawn 超时）。
# 可经 env KIMI_CCI_SPAWN_TIMEOUT 调（M1 ImageCache 预热后镜像秒拉，可调回小值）。
# 注：_wait_running 一旦 Running 立即返回，不会傻等满 timeout。
_SPAWN_TIMEOUT_S = int(os.environ.get("KIMI_CCI_SPAWN_TIMEOUT") or "180")
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
        metrics: MetricsState | None = None,
    ) -> None:
        self.client = client
        self.token_provider = token_provider
        self.endpoint = endpoint
        self.namespace = namespace
        self.region = region
        self.image = image
        self.image_pull_secret = image_pull_secret
        # Optional metrics sink (spec §7.2). Backfilled by app.py after the
        # MetricsState is built; None on the docker path / metrics-off, in which
        # case every emit point below is skipped. Instrumentation must NEVER let a
        # spawn / stop fail, so the helpers on MetricsState also swallow internally.
        # Assigning ``spawner.metrics`` also wires the REST client (api-error emit),
        # so app.py only has to set it once on the spawner.
        self._metrics: MetricsState | None = None
        self.metrics = metrics

    @property
    def metrics(self) -> MetricsState | None:
        return self._metrics

    @metrics.setter
    def metrics(self, value: MetricsState | None) -> None:
        self._metrics = value
        # Forward to the REST client so kimo_cci_api_error_total is emitted at the
        # single raise point inside CciRestClient (spec §7.2).
        self.client.metrics = value

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
        # spec §7.2: one kimo_sandbox_spawn_total{backend="cci",result} per attempt,
        # result ∈ {success, timeout, failure}; duration observed on success only
        # (a timed-out / errored spawn has no meaningful "Running+podIP ready" time
        # and would skew the latency histogram). active_sandboxes +1 on success.
        started = time.perf_counter()
        pod = self._build_pod_spec(sid, owner_id, env)
        try:
            resp = await self.client.create_pod(self.namespace, pod)
            name = resp.get("metadata", {}).get("name") or pod["metadata"]["name"]
            try:
                pod_ip = await self._wait_running(name, timeout=_SPAWN_TIMEOUT_S)
            except BaseException:
                # 超时/取消即删 Pod：keepalive 主进程(sleep infinity)不会自退，否则泄漏成
                # 永久 Running → 持续计费（2026-06-25 实测：超时的孤儿 Pod 一直跑）。
                import contextlib  # noqa: PLC0415

                from kimi_cli.web.spawner.cci_client import CciApiError  # noqa: PLC0415

                with contextlib.suppress(CciApiError):
                    await self.client.delete_pod(self.namespace, name, grace=0)
                raise
        except SpawnTimeout:
            self._record_spawn_result(_RESULT_TIMEOUT)
            raise
        except BaseException:
            # Any other failure (CciApiError on create/read, cancellation, etc.).
            self._record_spawn_result(_RESULT_FAILURE)
            raise
        # Success: count + observe latency + bump the active gauge.
        if self.metrics is not None:
            self.metrics.record_spawn(backend=_BACKEND, result=_RESULT_SUCCESS)
            self.metrics.observe_spawn_duration(
                backend=_BACKEND, seconds=time.perf_counter() - started
            )
            self.metrics.inc_active_sandboxes(1)
        return SandboxHandle(
            backend="cci",
            handle_id=name,
            network_endpoint=f"ws://{pod_ip}:{_WORKER_PORT}",
            created_at=self._now(),
            meta={"pod_ip": pod_ip, "namespace": self.namespace},
        )

    def _record_spawn_result(self, result: str) -> None:
        """Emit kimo_sandbox_spawn_total for a non-success spawn (None-safe)."""
        if self.metrics is not None:
            self.metrics.record_spawn(backend=_BACKEND, result=result)

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
                # Pod already gone → still no longer an active sandbox (spec §7.2).
                if self.metrics is not None:
                    self.metrics.inc_active_sandboxes(-1)
                return
            # Real delete failure: Pod may still be Running, don't drop the gauge.
            raise
        # Delete accepted → sandbox no longer active.
        if self.metrics is not None:
            self.metrics.inc_active_sandboxes(-1)

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
        metadata = {
            "name": f"kimo-sandbox-{sid}",
            "namespace": self.namespace,
            "labels": {
                "app": app_label,
                "owner": _label_safe(owner_id, "anon"),
                "session_id": _label_safe(str(sid)),
            },
            # 无 yangtse.io/* 注解: 网络由 namespace default Network 决定 (§4).
        }
        # hechun-fork-cci: CCI 镜像快照自动创建（加速冷启动；4.1GB 首拉 ~70s → 命中快照秒级）。
        # 首个 Pod 触发临时 cci-imagesnapshot Pod 制快照，之后匹配镜像直接用快照。
        # ⚠️ 安全前提：镜像须按**不可变**引用（push-swr 给 sandbox 打 :v<ts> 版本 tag +
        # render-env pin 到该版本）。否则 :latest 推新后快照不重建 → Pod 跑旧镜像。
        # env KIMI_CCI_IMAGE_SNAPSHOT=0 可关。
        if (os.environ.get("KIMI_CCI_IMAGE_SNAPSHOT", "1").strip().lower()
                not in {"0", "false", "no", ""}):
            metadata["annotations"] = {
                "cci.io/image-snapshot-create-if-not-present": "true",
            }
        pod = {
            "apiVersion": "cci/v2",
            "kind": "Pod",
            "metadata": metadata,
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
