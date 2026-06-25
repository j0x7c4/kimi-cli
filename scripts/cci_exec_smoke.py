#!/usr/bin/env python3
"""CCI 2.0 链路自包含 smoke：拉镜像 → Running → exec 101 → stdout 验。

# hechun-fork-cci  (spec 2026-06-24 §9-8 ★最大风险点的真机验收)

curl/postman 调不了 exec（官方明示），这一层只能对真实 Pod 验。本脚本是
M0 收尾"重跑 exec 实测"的可复用驱动（昨天是一次性 heredoc，没存档 → 补成仓内脚本）。

验收链路（每段打 [STAGE]，最后 PASS/FAIL 汇总）：
  1. SWEEP        清扫上次残留的 smoke Pod（label app=kimo-sandbox-smoke）
  2. CREATE       建 sleep Pod（image=SANDBOX_IMAGE，command 覆盖成 sleep 隔离 exec 通道）
  3. WAIT_RUNNING 轮询到 Running + podIP；非 Running 终态 dump 完整 status
                  → 区分 VPCEP 100.x 超时 / imagePullSecret 缺失 / 拉取鉴权失败
  4. EXEC         KimoExecStream.connect（IAM token → 101 握手 → channel.k8s.io）
  5. EXEC_OUTPUT  exec `uname/id/echo` → 读 channel-1 stdout → 验 sentinel
  6. CLEANUP      finally 删 Pod；signal.alarm 硬兜底（TaskStop 跳 finally 时也删）

凭证全走 env（不进 argv）：HUAWEICLOUD_AK/_SK/_CCI_REGION/_CCI_NAMESPACE、
SANDBOX_IMAGE、可选 SMOKE_PULL_SECRET（默认 swr-pull-secret，空字符串=不挂）、
可选 SWR_LOGIN_KEY（仅 ensure-secret 子命令用）。

用法（env 由 /app/scripts/cci-smoke.sh 从 secrets.yml[test] 注入）：
  uv run python scripts/cci_exec_smoke.py run            # 全链路
  uv run python scripts/cci_exec_smoke.py sweep          # 仅清扫残留
  uv run python scripts/cci_exec_smoke.py ensure-secret  # 建 swr-pull-secret（拉取鉴权缺失时）
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import sys
import time
import uuid

from kimi_cli.web.spawner.cci_auth import HuaweiSigner, TokenProvider
from kimi_cli.web.spawner.cci_client import CciApiError, CciRestClient
from kimi_cli.web.spawner.cci_exec import KimoExecStream

SMOKE_LABEL = "kimo-sandbox-smoke"
LABEL_SELECTOR = f"app={SMOKE_LABEL}"

# 4.1GB 镜像首拉可能慢；给足。整体 alarm 兜底另设。
WAIT_RUNNING_TIMEOUT_S = float(os.environ.get("SMOKE_WAIT_TIMEOUT", "300"))
EXEC_READ_DEADLINE_S = 45.0
MASTER_ALARM_S = int(os.environ.get("SMOKE_MASTER_ALARM", "420"))

# 拉镜像失败的 waiting.reason（终态，别傻等满 timeout）
_PULL_FAIL_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "RegistryUnavailable"}
_RUN_FAIL_REASONS = {"CrashLoopBackOff", "CreateContainerError", "RunContainerError"}


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s


def stage(name: str, msg: str = "") -> None:
    print(_c("36", f"[STAGE] {name}"), msg, flush=True)


def info(msg: str) -> None:
    print(f"        {msg}", flush=True)


def fail(msg: str) -> None:
    print(_c("31", f"[FAIL]  {msg}"), flush=True)


def ok(msg: str) -> None:
    print(_c("32", f"[ OK ]  {msg}"), flush=True)


def _env(*keys: str, required: bool = True, default: str | None = None) -> str:
    for k in keys:
        v = os.environ.get(k)
        if v:
            return v
    if required:
        fail(f"缺 env：{' / '.join(keys)}")
        sys.exit(2)
    return default or ""


def _build() -> tuple[CciRestClient, TokenProvider, str, str, str, str]:
    ak = _env("HUAWEICLOUD_AK")
    sk = _env("HUAWEICLOUD_SK")
    region = _env("HUAWEICLOUD_CCI_REGION")
    ns = _env("HUAWEICLOUD_CCI_NAMESPACE", default="kalinin-test", required=False) or "kalinin-test"
    endpoint = os.environ.get("HUAWEICLOUD_CCI_ENDPOINT") or f"cci.{region}.myhuaweicloud.com"
    image = _env("SANDBOX_IMAGE")
    client = CciRestClient(endpoint, HuaweiSigner(ak, sk))
    token_provider = TokenProvider(ak, sk, region)
    return client, token_provider, endpoint, ns, image, region


def _pod_spec(name: str, image: str, ns: str) -> dict:
    pull_secret = os.environ.get("SMOKE_PULL_SECRET", "imagepull-secret").strip()
    spec: dict = {
        "apiVersion": "cci/v2",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": {"app": SMOKE_LABEL},
        },
        "spec": {
            "containers": [
                {
                    "name": "sandbox",
                    "image": image,
                    "imagePullPolicy": "Always",
                    # 覆盖 entrypoint 成 sleep：隔离 exec 通道，不依赖 worker 行为。
                    "command": ["sleep"],
                    "args": ["600"],
                    "stdin": True,
                    "tty": False,
                    "resources": {
                        "requests": {"cpu": "2", "memory": "4Gi"},
                        "limits": {"cpu": "2", "memory": "4Gi"},
                    },
                }
            ],
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 5,
        },
    }
    if pull_secret:
        spec["spec"]["imagePullSecrets"] = [{"name": pull_secret}]
    return spec


def _summarize_status(pod: dict) -> str:
    st = pod.get("status", {})
    lines = [f"phase={st.get('phase')!r} podIP={st.get('podIP')!r}"]
    for c in st.get("containerStatuses", []) or []:
        state = c.get("state", {})
        for kind, body in state.items():
            reason = body.get("reason")
            message = body.get("message")
            lines.append(
                f"  container[{c.get('name')}] state={kind} "
                f"reason={reason!r} msg={message!r}"
            )
    for cond in st.get("conditions", []) or []:
        lines.append(
            f"  cond {cond.get('type')}={cond.get('status')} "
            f"{cond.get('reason') or ''} {cond.get('message') or ''}".rstrip()
        )
    return "\n".join(lines)


def _terminal_pull_failure(pod: dict) -> str | None:
    for c in pod.get("status", {}).get("containerStatuses", []) or []:
        w = c.get("state", {}).get("waiting", {})
        r = w.get("reason")
        if r in _PULL_FAIL_REASONS or r in _RUN_FAIL_REASONS:
            return f"{r}: {w.get('message')}"
    return None


# ── subcommands ───────────────────────────────────────────────────────────────


async def _sweep(client: CciRestClient, ns: str) -> int:
    pods = await client.list_pods(ns, label_selector=LABEL_SELECTOR)
    n = 0
    for p in pods:
        nm = p.get("metadata", {}).get("name")
        if not nm:
            continue
        try:
            await client.delete_pod(ns, nm, grace=0)
            info(f"deleted leftover pod {nm}")
            n += 1
        except CciApiError as e:
            if e.status != 404:
                info(f"delete {nm} → {e}")
    return n


async def cmd_sweep() -> int:
    client, _tp, _ep, ns, _img, _rg = _build()
    stage("SWEEP", f"ns={ns} selector={LABEL_SELECTOR}")
    n = await _sweep(client, ns)
    ok(f"swept {n} pod(s)")
    return 0


async def cmd_ensure_secret() -> int:
    """建/覆盖 swr-pull-secret（kubernetes.io/dockerconfigjson）—— 拉取鉴权缺失时用。

    core v1 secrets 走 /api/v1（非 cci/v2），用同一 AK/SK signer 直签 httpx。
    docker auth = base64("<region>@<AK>:<SWR_LOGIN_KEY>")。
    """
    import httpx

    ak = _env("HUAWEICLOUD_AK")
    sk = _env("HUAWEICLOUD_SK")
    region = _env("HUAWEICLOUD_CCI_REGION")
    ns = os.environ.get("HUAWEICLOUD_CCI_NAMESPACE") or "kalinin-test"
    login_key = _env("SWR_LOGIN_KEY")
    registry = f"swr.{region}.myhuaweicloud.com"
    secret_name = (os.environ.get("SMOKE_PULL_SECRET") or "swr-pull-secret").strip() or "swr-pull-secret"  # noqa: E501

    username = f"{region}@{ak}"
    auth_b64 = base64.b64encode(f"{username}:{login_key}".encode()).decode()
    dockercfg = {
        "auths": {registry: {"username": username, "password": login_key, "auth": auth_b64}}
    }
    dockercfg_b64 = base64.b64encode(json.dumps(dockercfg).encode()).decode()
    body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": secret_name, "namespace": ns},
        "type": "kubernetes.io/dockerconfigjson",
        "data": {".dockerconfigjson": dockercfg_b64},
    }
    signer = HuaweiSigner(ak, sk)
    endpoint = os.environ.get("HUAWEICLOUD_CCI_ENDPOINT") or f"cci.{region}.myhuaweicloud.com"
    base = f"https://{endpoint}/api/v1/namespaces/{ns}/secrets"
    raw = json.dumps(body).encode()

    stage("ENSURE_SECRET", f"{secret_name} @ ns={ns} registry={registry}")
    # 先尝试删旧（忽略 404），再建。
    del_url = f"{base}/{secret_name}"
    del_headers = signer.sign("DELETE", del_url, {"Content-Type": "application/json"}, b"")
    create_headers = signer.sign("POST", base, {"Content-Type": "application/json"}, raw)
    async with httpx.AsyncClient(timeout=30.0) as hc:
        dr = await hc.request("DELETE", del_url, headers=del_headers)
        info(f"DELETE old → {dr.status_code}")
        cr = await hc.request("POST", base, content=raw, headers=create_headers)
        if cr.status_code >= 400:
            fail(f"create secret → HTTP {cr.status_code}: {cr.text[:400]}")
            return 1
    ok(f"secret {secret_name} created/replaced")
    return 0


async def _with_pod(exec_fn) -> int:
    """sweep→建 sleep Pod→等 Running→exec_fn(endpoint,ns,name,tp)→finally 删 Pod。

    exec_fn 返回 rc（0=pass）。master alarm 兜底 + finally 保证删 Pod（含被
    SIGALRM 打断时）。run/diag 共用这套生命周期，删 Pod 逻辑只此一份。
    """
    client, token_provider, endpoint, ns, image, region = _build()
    name = f"kimo-smoke-{uuid.uuid4().hex[:12]}"
    info(f"endpoint={endpoint} ns={ns} region={region}")
    info(f"image={image}")
    info(f"pod={name}  pull_secret={os.environ.get('SMOKE_PULL_SECRET', 'imagepull-secret')!r}")

    # 全程 master alarm 兜底：即便阻塞在 WSClient C 调用里，SIGALRM 也能打断。
    created = {"yes": False}

    def _alarm(_sig, _frm):
        raise TimeoutError(f"master alarm {MASTER_ALARM_S}s 触发")

    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(MASTER_ALARM_S)

    rc = 1
    try:
        stage("SWEEP")
        await _sweep(client, ns)

        stage("CREATE", name)
        resp = await client.create_pod(ns, _pod_spec(name, image, ns))
        created["yes"] = True
        meta = resp.get("metadata", {})
        info(f"created: {meta.get('name')} uid={meta.get('uid')}")
        ok("pod accepted by cci/v2")

        stage("WAIT_RUNNING", f"timeout={WAIT_RUNNING_TIMEOUT_S:.0f}s")
        pod_ip = await _wait_running(client, ns, name)
        ok(f"Running, podIP={pod_ip}")

        rc = await exec_fn(endpoint, ns, name, token_provider)
    except CciApiError as e:
        fail(f"CCI API error: {e}")
    except TimeoutError as e:
        fail(str(e))
    except Exception as e:  # noqa: BLE001
        fail(f"{type(e).__name__}: {e}")
        import traceback

        traceback.print_exc()
    finally:
        signal.alarm(0)
        if created["yes"]:
            stage("CLEANUP", f"delete {name}")
            try:
                signal.alarm(60)
                await client.delete_pod(ns, name, grace=0)
                signal.alarm(0)
                ok("pod deleted")
            except CciApiError as e:
                if e.status == 404:
                    ok("pod already gone")
                else:
                    fail(f"cleanup delete failed: {e} —— ⚠️ 手动确认 ns={ns} 无残留 {name}")
            except Exception as e:  # noqa: BLE001
                fail(f"cleanup error: {e} —— ⚠️ 手动确认 ns={ns} 无残留 {name}")
    return rc


async def cmd_run() -> int:
    async def _exec_fn(endpoint, ns, name, tp):
        stage("EXEC", "open channel.k8s.io WebSocket (IAM token → 101 handshake)")
        out, ec = await _exec_probe(endpoint, ns, name, tp)
        stage("EXEC_OUTPUT")
        for ln in out.splitlines():
            info(f"stdout| {ln}")
        info(f"exec returncode={ec}")
        if "HELLO_FROM_CCI_POD" in out and "CCI_EXEC_DONE" in out:
            ok("exec stdout sentinel matched — 101 握手 + channel.k8s.io 分帧通")
            return 0
        fail("exec stdout 未含 sentinel（握手/分帧/命令异常）")
        return 1

    rc = await _with_pod(_exec_fn)
    print()
    banner = _c("32", "===== SMOKE PASS =====") if rc == 0 else _c("31", "===== SMOKE FAIL =====")
    print(banner, flush=True)
    return rc


# 整链前置体检脚本（真镜像 sleep Pod 内 exec 跑）：验 worker 可导入 + LLM/公网
# egress（整链最致命未知：Pod 无 NAT/SNAT → worker 调不到 api.moonshot.cn）。
_DIAG_SCRIPT = r"""
echo CCI_DIAG_BEGIN
echo "[os] $(head -1 /etc/os-release 2>/dev/null); arch=$(uname -m)"
echo "[py] $(python3 --version 2>&1)"
W='import kimi_cli.web.runner.worker as w; print("OK", w.__file__)'
printf '[worker-import] '; python3 -c "$W" 2>&1 | tail -1
printf '[app-ls] '; ls /app 2>/dev/null | tr '\n' ' '; echo
C='import socket,sys; socket.create_connection((sys.argv[1],443),timeout=8); print("connect OK")'
printf '[egress moonshot:443] '; python3 -c "$C" api.moonshot.cn 2>&1 | tail -1
printf '[egress pypi:443] '; python3 -c "$C" pypi.org 2>&1 | tail -1
echo CCI_DIAG_DONE
"""


async def cmd_diag() -> int:
    """整链前置体检：真镜像 sleep Pod 内 exec，验 worker 可导入 + Pod→代理可达。

    设 ``SMOKE_PROXY_URL``（如 http://192.168.0.50:4000）则在 Pod 内探代理 health —— 这是
    "worker 经 in-VPC 代理出网"路线的关键验收（公网 egress 探测仍保留作对照，预期不通）。
    """
    proxy = os.environ.get("SMOKE_PROXY_URL", "").strip().rstrip("/")
    script = _DIAG_SCRIPT
    if proxy:
        probe = (
            "P='import urllib.request as u,sys; "
            'print(u.urlopen(sys.argv[1]+"/health/liveliness",timeout=8).read().decode())\'\n'
            f"printf '[proxy {proxy}] '; python3 -c \"$P\" {proxy} 2>&1 | tail -1\n"
        )
        script = script.replace("echo CCI_DIAG_DONE", probe + "echo CCI_DIAG_DONE")

    async def _exec_fn(endpoint, ns, name, tp):
        stage("EXEC", "diag: worker-import / Pod→proxy / public egress(对照)")
        out, ec = await _exec_probe(
            endpoint, ns, name, tp,
            cmd=["/bin/sh", "-c", script], done_marker="CCI_DIAG_DONE",
        )
        stage("DIAG_OUTPUT")
        for ln in out.splitlines():
            info(f"  {ln}")
        info(f"exec returncode={ec}")
        ok_worker = "[worker-import] OK" in out
        ok_proxy = bool(proxy) and f"[proxy {proxy}]" in out and "alive" in out.lower()
        verdict = f"判定：worker-import={'✅' if ok_worker else '❌'}"
        verdict += (f"  Pod→proxy={'✅' if ok_proxy else '❌'}" if proxy
                    else "  (未设 SMOKE_PROXY_URL，跳过代理探测)")
        info(verdict)
        return 0 if (ok_worker and (ok_proxy or not proxy)) else 1

    rc = await _with_pod(_exec_fn)
    print()
    banner = (_c("32", "===== DIAG PASS =====") if rc == 0
              else _c("33", "===== DIAG INCOMPLETE ====="))
    print(banner, flush=True)
    return rc


async def _wait_running(client: CciRestClient, ns: str, name: str) -> str:
    deadline = time.monotonic() + WAIT_RUNNING_TIMEOUT_S
    delay = 2.0
    last_phase = None
    while time.monotonic() < deadline:
        pod = await client.read_pod(ns, name)
        phase = pod.get("status", {}).get("phase")
        if phase != last_phase:
            info(f"phase → {phase}")
            last_phase = phase
        if phase == "Running":
            ip = pod.get("status", {}).get("podIP")
            if ip:
                return ip
        if phase in ("Failed", "Succeeded"):
            print(_summarize_status(pod), flush=True)
            raise RuntimeError(f"pod terminal phase={phase} before Running")
        tf = _terminal_pull_failure(pod)
        if tf:
            print(_summarize_status(pod), flush=True)
            fail(f"拉镜像/起容器失败 → {tf}")
            info("诊断：'dial 100.x timeout'=VPCEP 缺/挂；"
                 "'not found'=imagePullSecret 名错(应 imagepull-secret)；"
                 "'401/denied'=拉取鉴权错；'no such host'=DNS/OBS VPCEP")
            raise RuntimeError(f"image pull/container failure: {tf}")
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 8.0)
    # 超时：dump 最后状态
    try:
        pod = await client.read_pod(ns, name)
        print(_summarize_status(pod), flush=True)
    except CciApiError:
        pass
    raise TimeoutError(f"pod 未在 {WAIT_RUNNING_TIMEOUT_S:.0f}s 内 Running")


async def _exec_probe(
    endpoint: str, ns: str, name: str, token_provider: TokenProvider,
    *, cmd: list[str] | None = None, done_marker: str = "CCI_EXEC_DONE",
) -> tuple[str, int | None]:
    if cmd is None:
        cmd = [
            "/bin/sh",
            "-c",
            "echo CCI_EXEC_BEGIN; uname -a; id; "
            "(head -1 /etc/os-release 2>/dev/null || true); "
            "echo HELLO_FROM_CCI_POD; echo CCI_EXEC_DONE",
        ]
    stream = KimoExecStream()
    info("fetching IAM token (hw_ak_sk getToken)…")
    await stream.connect(endpoint, ns, name, token_provider, command=cmd)
    ok("WebSocket connected (101 Switching Protocols)")

    collected = bytearray()
    marker = done_marker.encode()
    deadline = time.monotonic() + EXEC_READ_DEADLINE_S
    try:
        while time.monotonic() < deadline:
            line = await stream.readline()
            if line == b"":
                break  # EOF
            collected.extend(line)
            if marker in bytes(line):
                # 给 stderr/error channel 一点时间 flush returncode
                break
        ec = stream.returncode()
        err = stream.read_error()
        if err:
            info(f"error-channel: {err.decode('utf-8', 'replace')[:300]}")
        return collected.decode("utf-8", "replace"), ec
    finally:
        await stream.close()


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    table = {
        "run": cmd_run, "diag": cmd_diag,
        "sweep": cmd_sweep, "ensure-secret": cmd_ensure_secret,
    }
    fn = table.get(cmd)
    if fn is None:
        fail(f"未知子命令 {cmd!r}（run|diag|sweep|ensure-secret）")
        return 2
    return asyncio.run(fn())


if __name__ == "__main__":
    sys.exit(main())
