"""Worker module for running KimiCLI in a subprocess.

This module is the entry point for the subprocess that runs KimiCLI in wire mode.
It reads the session configuration from disk and runs KimiCLI.run_wire_stdio().

Usage:
    python -m kimi_cli.web.runner.worker <session_id>
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

# hechun-diag(warmpool M0): 纯诊断计时。目的是量出 worker 冷启动各段真实耗时占比
# （spec 2026-09-07-sandbox-warmpool-design.md §1 的「worker_load 23s 大头是什么」从未测量）。
# 只做打点 + 一行结构化日志，不改任何控制流 / 默认值。
_DIAG: dict[str, float] = {}


def _diag_proc_age_ms() -> float:
    """本进程从 fork/exec 到现在的毫秒数（Linux /proc）。

    用来把「解释器启动 + 本模块 import」这段（发生在 main() 之前，perf_counter 抓不到起点）
    量出来。非 Linux / 读失败一律返回 -1，绝不抛。
    """
    try:
        with open("/proc/self/stat", encoding="utf-8") as f:
            raw = f.read()
        # comm 字段可能含空格/括号，按最后一个 ')' 之后切
        fields = raw[raw.rindex(")") + 2 :].split()
        starttime_ticks = float(fields[19])  # field 22 (1-based) = starttime
        hz = float(os.sysconf("SC_CLK_TCK"))
        with open("/proc/uptime", encoding="utf-8") as f:
            uptime_s = float(f.read().split()[0])
        return (uptime_s - starttime_ticks / hz) * 1000.0
    except Exception:  # noqa: BLE001 — 诊断代码绝不能影响启动
        return -1.0


# 本模块 body 开始执行的时刻：此前是解释器启动 + 上面几个 stdlib import。
_DIAG["t_module_begin"] = time.perf_counter()
_DIAG["proc_age_at_module_begin_ms"] = _diag_proc_age_ms()

from kimi_cli import logger  # noqa: E402
from kimi_cli.agentspec import resolve_subagent_yaml  # noqa: E402
from kimi_cli.app import KimiCLI, enable_logging  # noqa: E402
from kimi_cli.cli.mcp import get_global_mcp_config_file  # noqa: E402
from kimi_cli.exception import MCPConfigError  # noqa: E402
from kimi_cli.web.runner.mcp_discovery import load_auto_discovered_mcp_configs  # noqa: E402
from kimi_cli.web.store.sessions import load_session_by_id  # noqa: E402

# 重模块 import（KimiCLI / agentspec / web store …）结束。
_DIAG["t_module_imports_done"] = time.perf_counter()

# hechun-fork-cci: dedicated worker exit code for "the required agent could not
# be loaded, refusing to start". The gateway (CCISessionProcess) recognises this
# specific code on worker EOF → records kimo_agent_load_failure_total + broadcasts
# a clear client-visible error, instead of treating it like a generic crash. Any
# value outside the normal 0/1/130/137… range works; 42 is unambiguous.
AGENT_LOAD_FAILURE_EXIT_CODE = 42

# reason label values for kimo_agent_load_failure_total{reason} (mirrors
# MetricsState.record_agent_load_failure). Kept here next to the raise sites so
# the worker can stamp the reason onto the wire error it emits before exiting.
_REASON_SUBAGENT_UNRESOLVED = "subagent_unresolved"
_REASON_AGENT_REQUIRED_MISSING = "agent_required_missing"


class SubagentNotFoundError(RuntimeError):
    """Raised when the sandbox is launched with a ``SUBAGENT`` env var that
    does not resolve to an agent spec yaml on disk.

    The sandbox worker treats this as fatal — silently falling back to the
    default agent would hide a misconfiguration (e.g. hechun broker injected
    ``SUBAGENT=diabetes-expert`` but the skill bundle wasn't mounted) and let
    the wrong system prompt + tool set serve real users.
    """

    #: metric reason label stamped when this error aborts worker startup.
    reason = _REASON_SUBAGENT_UNRESOLVED


class AgentRequiredError(RuntimeError):
    """Raised when ``KIMI_REQUIRE_AGENT`` is truthy but agent resolution still
    landed on the default agent (``agent_file is None``).

    The gateway sets ``KIMI_REQUIRE_AGENT=1`` whenever it forwards a specific
    agent name (``SUBAGENT``) for a session — i.e. this session *requires* that
    agent. Falling back to the default agent here would silently serve users the
    wrong system prompt + tool set, so the worker refuses to start instead. This
    is distinct from :class:`SubagentNotFoundError` (which fires when a
    ``SUBAGENT`` name was set but unresolvable): ``AgentRequiredError`` guards the
    case where no concrete agent was selected at all yet one was demanded.
    """

    #: metric reason label stamped when this error aborts worker startup.
    reason = _REASON_AGENT_REQUIRED_MISSING


def _is_truthy_env(name: str) -> bool:
    """Whether env var ``name`` is set to a truthy value (1/true/yes)."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# hechun-fork-cci: env switch the gateway injects (alongside SUBAGENT) to demand
# fail-fast agent loading. Absent on docker/local → worker keeps the old
# fall-back-to-default behaviour, so this is fully backward compatible.
_REQUIRE_AGENT_ENV = "KIMI_REQUIRE_AGENT"


# hechun-fork-cci: env names the gateway injects into a CCI Pod worker so it can
# pull static assets the Pod can't bind-mount (see web/api/sandbox_assets.py and
# web/runner/cci_process.py). Absent on the docker path → worker skips download.
_ASSETS_URL_ENV = "KIMO_SANDBOX_ASSETS_URL"
_ASSETS_TOKEN_ENV = "KIMO_SANDBOX_ASSETS_TOKEN"

# hechun-fork-cci (warm pool): opt-in gate for the cold-start timing frame on
# stdout. Off by default so production workers keep a pure JSON-RPC stdout.
_TIMING_ENV = "KIMO_WORKER_TIMING"

# Mirrors of the warm-protocol reason labels, imported lazily below so this
# module keeps its import surface unchanged on the docker/local path.
_REASON_ASSETS_MISSING_LABEL = "assets_missing"
_REASON_AGENT_UNRESOLVED_LABEL = "agent_unresolved"


def _is_safe_tar_member(name: str) -> bool:
    """Reject absolute paths and ``..`` traversal in a tar member name.

    The bundle is produced by our own gateway, but the worker still validates
    before extracting under ``$HOME`` so a compromised/garbled response can't
    write outside the home dir.
    """
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    parts = Path(name).parts
    return ".." not in parts


def _fetch_sandbox_assets() -> None:
    """Download + unpack the gateway sandbox-assets bundle under ``$HOME``.

    CCI serverless Pods cannot bind-mount the gateway host, so static files the
    worker resolves off local disk (``~/.kimi/agents`` custom agent specs, and
    later a knowledge base etc.) are fetched over HTTP from the gateway's
    internal endpoint and extracted into ``$HOME`` before agent resolution runs.

    No-op when ``KIMO_SANDBOX_ASSETS_URL`` is unset (the docker path never
    injects it; its bind-mount is the source there). Download/extract failures
    are logged loudly but do NOT raise: this is the critical path for CCI agent
    config, and an opaque crash here would surface downstream as a misleading
    ``SubagentNotFoundError``, so we keep the explicit "failed to fetch sandbox
    assets from gateway" log as the breadcrumb.
    """
    url = os.environ.get(_ASSETS_URL_ENV, "").strip()
    if not url:
        return

    import io  # noqa: PLC0415
    import tarfile  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    token = os.environ.get(_ASSETS_TOKEN_ENV, "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    from kimi_cli.web.api.sandbox_assets import KNOWLEDGE_BUNDLE_PREFIX  # noqa: PLC0415

    home = Path.home()
    # Knowledge base unpacks under the session work_dir (load_knowledge_base +
    # the agent's ReadFile both resolve relative to work_dir); agents (and any
    # other member) unpack under $HOME where discover_user_agent_specs also
    # searches. work_dir mirrors run_worker's KIMI_WORK_DIR-or-/app logic.
    work_dir = Path(os.environ.get("KIMI_WORK_DIR") or "/app")

    def _is_kb(name: str) -> bool:
        return name == KNOWLEDGE_BUNDLE_PREFIX or name.startswith(KNOWLEDGE_BUNDLE_PREFIX + "/")

    try:
        _t_http0 = time.perf_counter()
        resp = httpx.get(url, headers=headers, timeout=30.0)
        resp.raise_for_status()
        data = resp.content
        # hechun-diag(warmpool M0): 静态资源「下载」与「解包」分开计时 —— 这正是
        # spec §1 待验证的那一项（下载到底占 worker_load 多少）。
        _DIAG["fetch_http_ms"] = (time.perf_counter() - _t_http0) * 1000.0
        _DIAG["fetch_bytes"] = float(len(data))
        _t_x0 = time.perf_counter()
        n_kb = 0
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            members = [m for m in tar.getmembers() if _is_safe_tar_member(m.name)]
            rejected = len(tar.getmembers()) - len(members)
            if rejected:
                logger.warning(
                    "[sandbox-assets] rejected {n} unsafe tar member(s) (path traversal)",
                    n=rejected,
                )
            for m in members:
                if _is_kb(m.name):
                    tar.extract(m, path=work_dir)  # noqa: S202 — members filtered above
                    n_kb += 1
                else:
                    tar.extract(m, path=home)  # noqa: S202 — members filtered above
        logger.info(
            "[sandbox-assets] fetched + extracted bundle ({size} bytes, {n} members; "
            "{nkb} knowledge->{wd}, rest->{home})",
            size=len(data),
            n=len(members),
            nkb=n_kb,
            wd=str(work_dir),
            home=str(home),
        )
        _DIAG["fetch_extract_ms"] = (time.perf_counter() - _t_x0) * 1000.0
    except Exception as e:  # noqa: BLE001 — must not crash worker startup
        logger.error(
            "[sandbox-assets] failed to fetch sandbox assets from gateway url={url}: {err}. "
            "Custom agent config (e.g. SUBAGENT yaml) may be missing on this Pod.",
            url=url,
            err=e,
        )


def _emit_worker_timing(session_id: UUID) -> None:
    """Emit one worker cold-start timing line. Diagnostic only — never affects control flow.

    Two sinks, both best-effort (every exception swallowed):

    1. ``logger.info`` → the Pod's ``kimi.log``.
    2. **Only when ``KIMO_WORKER_TIMING`` is truthy** — one ``kimo_diag`` frame on
       stdout. The gateway read loop recognises that key, logs it and consumes it
       (``process.py`` ``_is_diag_frame``): it is neither broadcast to WebSocket
       clients nor fed to the JSON-RPC validator.

    Why stdout and not stderr: ``enable_logging`` dup2's fd 2 into ``kimi.log``,
    and the CCI exec stderr channel is only drained by the gateway when the worker
    exits — so stderr carries no live signal out of the Pod. stdout is the one
    live channel, hence the explicit gate + the dedicated frame shape (the M0
    diagnostic build wrote a bare text line here, which the gateway could only
    surface as a misleading ``Invalid JSONRPC out message``).

    Aligned with the gateway's ``cci_process._log_cold_start_timing``:
    ``worker_load`` is anchored on the first non-empty stdout line, so
    fetch+import+session+mcp+create should sum to roughly that number.
    """
    try:
        d = _DIAG

        def seg(a: str, b: str) -> float:
            if a not in d or b not in d:
                return -1.0
            return (d[b] - d[a]) * 1000.0

        interp_ms = d.get("proc_age_at_module_begin_ms", -1.0)
        import_ms = seg("t_module_begin", "t_module_imports_done")
        # module import 结束 → run_worker 开始（main() 里的 enable_logging / proctitle 等）
        pre_ms = seg("t_module_imports_done", "t_run_begin")
        fetch_ms = seg("t_run_begin", "t_fetch_done")
        session_ms = seg("t_fetch_done", "t_session_done")
        mcpcfg_ms = seg("t_session_done", "t_mcpcfg_done")
        agent_ms = seg("t_mcpcfg_done", "t_agent_resolve_done")
        create_ms = seg("t_agent_resolve_done", "t_create_done")
        run_total_ms = seg("t_run_begin", "t_create_done")
        proc_total_ms = _diag_proc_age_ms()
        # KimiCLI.create 内部已有的分段（config / runtime init / load_agent+MCP 建连），
        # 由 app.py 末尾暴露成模块级变量；原本只进 telemetry，日志里看不到。
        import kimi_cli.app as _kapp  # noqa: PLC0415

        _cp = getattr(_kapp, "_LAST_CREATE_PHASE_TIMINGS_MS", {}) or {}
        line = (
            "[kimo][worker-timing] sid={sid} interp_boot={interp}ms import={imp}ms "
            "pre_run={pre}ms fetch={fetch}ms (http={fhttp}ms {fbytes}B extract={fx}ms) "
            "session={sess}ms mcp_cfg={mcp}ms agent_resolve={agent}ms create={create}ms "
            "run_worker_total={rt}ms proc_total={pt}ms "
            "| create_breakdown: config={c_cfg}ms runtime_init={c_init}ms "
            "load_agent_mcp={c_mcp}ms create_total={c_tot}ms"
        ).format(
            sid=str(session_id),
            interp=int(interp_ms),
            imp=int(import_ms),
            pre=int(pre_ms),
            fetch=int(fetch_ms),
            fhttp=int(d.get("fetch_http_ms", -1.0)),
            fbytes=int(d.get("fetch_bytes", -1.0)),
            fx=int(d.get("fetch_extract_ms", -1.0)),
            sess=int(session_ms),
            mcp=int(mcpcfg_ms),
            agent=int(agent_ms),
            create=int(create_ms),
            rt=int(run_total_ms),
            pt=int(proc_total_ms),
            c_cfg=_cp.get("config_ms", -1),
            c_init=_cp.get("init_ms", -1),
            c_mcp=_cp.get("mcp_ms", -1),
            c_tot=_cp.get("total_ms", -1),
        )
        logger.info(line)
        if _is_truthy_env(_TIMING_ENV):
            sys.stdout.write(
                json.dumps(
                    {
                        "kimo_diag": "worker_timing",
                        "sid": str(session_id),
                        "line": line,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            sys.stdout.flush()
    except Exception:  # noqa: BLE001 — 诊断绝不能弄崩 worker 启动
        pass


class PrepareResult:
    """Outcome of the session-independent (pre-bind) half of worker startup.

    ``ok`` is what the gateway turns into ``warming → ready``. It is deliberately
    stricter than "the process started": :func:`_fetch_sandbox_assets` logs
    download failures loudly but does **not** raise (an opaque crash there used
    to surface downstream as a misleading ``SubagentNotFoundError``), so a Pod
    whose agent yaml never arrived would otherwise be advertised as ready, sit in
    the pool forever and fail every single claim. Hence the explicit
    "did the agent spec actually land on disk" check.
    """

    __slots__ = ("ok", "reason", "agent_name", "agent_path", "prepare_ms")

    def __init__(
        self,
        *,
        ok: bool,
        reason: str | None = None,
        agent_name: str = "",
        agent_path: str | None = None,
        prepare_ms: float = -1.0,
    ) -> None:
        self.ok = ok
        self.reason = reason
        self.agent_name = agent_name
        self.agent_path = agent_path
        self.prepare_ms = prepare_ms


def _verify_static_assets() -> PrepareResult:
    """Check that the static prep actually produced what a claim will need.

    Only meaningful when ``SUBAGENT`` names a required agent (the gateway sets it
    for every hechun session, warm Pods included). Without it there is nothing
    session-independent left to verify, so the result is ``ok``.
    """
    agent_name = os.environ.get("SUBAGENT", "").strip()
    if not agent_name:
        return PrepareResult(ok=True)
    work_dir = Path(os.environ.get("KIMI_WORK_DIR") or "/app")
    try:
        path = resolve_subagent_yaml(agent_name, work_dir=work_dir)
    except Exception as e:  # noqa: BLE001 — a resolution crash is a failed prep, not a crash
        logger.error(
            "[warm] agent resolution raised while verifying static assets "
            "(SUBAGENT={name}): {err}",
            name=agent_name,
            err=e,
        )
        return PrepareResult(ok=False, reason=_REASON_AGENT_UNRESOLVED_LABEL, agent_name=agent_name)
    if path is None:
        logger.error(
            "[warm] static assets incomplete: SUBAGENT={name} did not resolve to an "
            "agent yaml after the sandbox-assets fetch; refusing to report ready.",
            name=agent_name,
        )
        return PrepareResult(ok=False, reason=_REASON_ASSETS_MISSING_LABEL, agent_name=agent_name)
    return PrepareResult(ok=True, agent_name=agent_name, agent_path=str(path))


async def prepare_static() -> PrepareResult:
    """Phase 1: everything that is independent of session / owner identity.

    hechun-fork-cci: on the CCI path the Pod can't bind-mount the gateway host,
    so pull static assets (~/.kimi/agents custom agent specs, knowledge base…)
    from the gateway and unpack them under $HOME BEFORE agent resolution consumes
    them. No-op on docker/local (env not injected). Run off-thread: httpx.get is
    blocking and the caller drives an asyncio event loop.

    🔴 The phase boundary stops HERE and may not move one step further: the next
    step consumes the session id, and everything below ``KimiCLI.create``
    substitutes ``${KIMI_USER_ID}`` from ``os.environ`` into MCP headers. Building
    those connections before a user is bound would attach an empty identity to a
    connection that is never rebound (spec §4.4 — security, not latency).
    """
    _DIAG["t_run_begin"] = time.perf_counter()
    await asyncio.to_thread(_fetch_sandbox_assets)
    _DIAG["t_fetch_done"] = time.perf_counter()
    result = _verify_static_assets()
    result.prepare_ms = (_DIAG["t_fetch_done"] - _DIAG["t_run_begin"]) * 1000.0
    return result


async def run_worker(session_id: UUID) -> None:
    """Run the KimiCLI worker for a session (cold path: prepare then bind at once)."""
    await prepare_static()
    await bind_and_run(session_id)


async def bind_and_run(
    session_id: UUID,
    *,
    owner_id: str | None = None,
    yolo: bool | None = None,
) -> None:
    """Phase 2: bind this worker to a real session and run it.

    ``owner_id`` / ``yolo`` are supplied only on the warm-pool path, where they
    arrive in the bind frame — the gateway holds the authoritative values, and on
    CCI the worker's own storage cannot read them back (``RemoteKimoStorage``'s
    ``load_session_state`` is inert by design). They are written into
    ``os.environ`` here, i.e. *before* any MCP config is loaded, so the existing
    env-driven resolution below sees exactly the identity that was bound. On the
    cold path both are ``None`` and the pre-existing env/storage logic is
    untouched.
    """
    if owner_id is not None:
        os.environ["KIMI_USER_ID"] = owner_id
    if yolo is not None:
        os.environ["KIMO_DEFAULT_YOLO"] = "1" if yolo else "0"
    if owner_id is not None or yolo is not None:
        os.environ["KIMI_SESSION_ID"] = str(session_id)

    # Find session by ID using the web store (disk-based registry).
    joint_session = load_session_by_id(session_id)
    if joint_session is not None:
        session = joint_session.kimi_cli_session
    else:
        # hechun-fork-cci: CCI serverless Pod 无法 bind-mount gateway 宿主的 session
        # 目录（docker 路径靠 bind-mount，container.py:274-281），故 worker 在 Pod 内
        # load_session_by_id 必然为 None。用同一 session_id + env 的 work_dir 本地构造
        # 新 session（context 空起；session state / memory 走 storage 后端 MySQL 持久化）。
        # 新会话即可用；历史 resume 不跨 Pod（MVP 取舍，2026-06-25 用户拍板方案③）。
        from kaos.path import KaosPath  # noqa: PLC0415

        from kimi_cli.session import Session as KimiCLISession  # noqa: PLC0415

        work_dir_env = os.environ.get("KIMI_WORK_DIR") or "/app"
        work_dir = KaosPath.unsafe_from_local_path(Path(work_dir_env))
        logger.info(
            "Session {sid} not on Pod disk (CCI no bind-mount); constructing fresh "
            "session from env at work_dir={wd}",
            sid=str(session_id),
            wd=work_dir_env,
        )
        session = await KimiCLISession.create(work_dir=work_dir, session_id=str(session_id))
        # CCI fresh session 的 state 是空的（create() → SessionState()，approval.yolo=False，
        # refresh() 只补 title）。gateway 在 create_session 已把权威 state（approval.yolo +
        # auto-approves + owner_id）写进 storage 后端（mysql，kimo_session_state 行，
        # sessions.py:437/440）。这里读回来覆盖到 fresh session.state —— 否则 yolo 丢失，
        # Memory.add(persistent) 要审批，而 iOS/Flutter 简化 UI 无审批路径 → 永久卡死
        # （2026-06-25 实测；effective_yolo = yolo or session.state.approval.yolo，agent.py）。
        import contextlib  # noqa: PLC0415

        owner_env = os.environ.get("KIMI_USER_ID") or None
        try:
            from kimi_cli.session_state import load_session_state_via_storage  # noqa: PLC0415
            from kimi_cli.storage import build_storage  # noqa: PLC0415

            _persisted = load_session_state_via_storage(session_id, build_storage())
            session.state.approval = _persisted.approval  # yolo / afk / auto_approve_actions
            if _persisted.owner_id:
                session.state.owner_id = _persisted.owner_id
            logger.info(
                "CCI fresh session loaded persisted state from storage: yolo={y} owner={o}",
                y=session.state.approval.yolo,
                o=session.state.owner_id,
            )
        except Exception as _e:  # noqa: BLE001 — storage miss/err 不阻塞启动
            logger.warning(
                "CCI fresh session storage-state load failed ({err}); env owner fallback only",
                err=_e,
            )
        # owner_id env 兜底（storage 无 owner / 非 mysql 后端时）。
        if owner_env and not session.state.owner_id:
            with contextlib.suppress(Exception):
                session.state.owner_id = owner_env

        # yolo env 兜底（storage 读失败 / Pod 无 mysql 凭证时）。CCI fresh session 若 storage
        # 读回失败，yolo 停在 False → MCP/Memory 等工具调用卡在 approval.request 的无限 await
        # （iOS/Flutter 简化 UI 无审批路径 → 永久卡死，见上方注释 + soul/approval.py）。
        # gateway 的 KIMO_DEFAULT_YOLO 经 _SANDBOX_ENV_VARS 转发进 Pod，这里作权威兜底。
        if (
            os.environ.get("KIMO_DEFAULT_YOLO", "").lower() in {"1", "true", "yes"}
            and not session.state.approval.yolo
        ):
            session.state.approval.yolo = True
            logger.info("CCI fresh session yolo=True from KIMO_DEFAULT_YOLO env fallback")

    _DIAG["t_session_done"] = time.perf_counter()

    # Load default MCP config file if it exists
    default_mcp_file = get_global_mcp_config_file()
    mcp_configs: list[dict[str, Any]] = []
    if default_mcp_file.exists():
        raw = default_mcp_file.read_text(encoding="utf-8")
        try:
            mcp_configs = [json.loads(raw)]
        except json.JSONDecodeError:
            logger.warning(
                "Invalid JSON in MCP config file: {path}",
                path=default_mcp_file,
            )

    # hechun (avocado) integration: also load any auto-discovered MCP
    # configs from ``~/.config/agents/mcp.json`` (the hechun bundle's
    # mount target).  Done after the global config so a deliberate
    # global entry can still shadow an auto-discovered one if names
    # collide (the existing kimi MCP loader is first-write-wins).
    #
    # ``${VAR}`` placeholders in the auto-discovered file are
    # substituted from ``os.environ`` here so a single yaml shipped
    # with the skill bundle can serve different sessions (different
    # ``HECHUN_MCP_TOKEN``s, etc.) without per-session rendering.
    auto_mcp_configs = load_auto_discovered_mcp_configs()
    if auto_mcp_configs:
        mcp_configs.extend(auto_mcp_configs)
    _DIAG["t_mcpcfg_done"] = time.perf_counter()

    # Detect whether this is a resumed session (has prior state on disk)
    # vs a brand-new session that should honor config.default_plan_mode.
    resumed = (session.dir / "state.json").exists()

    # Read per-session config (thinking override, agent spec path);
    # None → falls back to global config / default agent.
    session_thinking: bool | None = None
    agent_file: Path | None = None
    _agent_spec_path: str | None = None
    _cfg_file = session.dir / "session_config.json"
    if _cfg_file.exists():
        try:
            _cfg = json.loads(_cfg_file.read_text(encoding="utf-8"))
            session_thinking = _cfg.get("thinking")
            _agent_spec_path = _cfg.get("agent_spec_path")
        except Exception:
            pass
    if _agent_spec_path:
        _path = Path(_agent_spec_path)
        if not _path.is_file():
            raise FileNotFoundError(
                f"Agent spec file recorded in session_config.json no longer exists: {_path}"
            )
        agent_file = _path

    # hechun (avocado) integration: when the gateway started this sandbox
    # with ``SUBAGENT=<name>``, look up the matching subagent yaml and use it
    # as the top-level agent spec for this session.
    #
    # Resolution order: ``~/.config/agents/skills/*/subagents/<name>.yaml``
    # (the canonical sandbox-mount path; see spec §3.4) wins over the
    # generic ``~/.kimi/agents`` shape so a hechun-bundled spec always beats
    # a stray user-level file with the same name.
    #
    # Fail-fast on miss: silently falling back to the default agent would
    # hide a real misconfiguration (broker selected ``diabetes-expert`` but
    # the bundle wasn't mounted) and serve users the wrong system prompt.
    subagent_name = os.environ.get("SUBAGENT", "").strip()
    if subagent_name:
        subagent_path = resolve_subagent_yaml(subagent_name, work_dir=Path(str(session.work_dir)))
        if subagent_path is None:
            logger.error(
                "SUBAGENT={name} requested but no matching agent yaml found "
                "(checked ~/.config/agents/skills/*/subagents/{name}.yaml, "
                "~/.config/agents/subagents/{name}.yaml, and "
                "discover_user_agent_specs). Refusing to fall back silently.",
                name=subagent_name,
            )
            raise SubagentNotFoundError(
                f"SUBAGENT={subagent_name!r} did not resolve to an agent spec; "
                "check that the skill bundle is mounted at "
                f"~/.config/agents/skills/<bundle>/subagents/{subagent_name}.yaml."
            )
        logger.info(
            "SUBAGENT={name} resolved to {path}",
            name=subagent_name,
            path=subagent_path,
        )
        agent_file = subagent_path

    # hechun-fork-cci: fail-fast guard (KIMI_REQUIRE_AGENT). The gateway sets
    # this whenever it forwarded a specific agent name for this session, so the
    # session REQUIRES that agent. If every resolution path above still left
    # agent_file None (e.g. the SUBAGENT env got dropped, or the assets bundle
    # never delivered the yaml), we'd otherwise fall back to the default agent and
    # silently serve the wrong agent. Refuse to start instead — main() turns this
    # into a clear client-visible wire error + AGENT_LOAD_FAILURE_EXIT_CODE.
    if agent_file is None and _is_truthy_env(_REQUIRE_AGENT_ENV):
        logger.error(
            "{env} is set but agent resolution produced no agent_file "
            "(SUBAGENT={sub!r}); refusing to start with the default agent.",
            env=_REQUIRE_AGENT_ENV,
            sub=subagent_name,
        )
        raise AgentRequiredError(
            f"{_REQUIRE_AGENT_ENV} is set (this session requires a specific agent) "
            f"but none was resolved (SUBAGENT={subagent_name!r}). Refusing to fall "
            "back to the default agent. Check that the gateway forwarded SUBAGENT "
            "and that the agent yaml was delivered to this Pod "
            "(~/.kimi/agents/<name>.yaml via the sandbox-assets bundle)."
        )

    _DIAG["t_agent_resolve_done"] = time.perf_counter()

    # Create KimiCLI instance with MCP configuration
    try:
        kimi_cli = await KimiCLI.create(
            session,
            mcp_configs=mcp_configs or None,
            resumed=resumed,
            ui_mode="wire",
            thinking=session_thinking,
            agent_file=agent_file,
        )
    except MCPConfigError as exc:
        logger.warning(
            "Invalid MCP config in {path}: {error}. Starting without MCP.",
            path=default_mcp_file,
            error=exc,
        )
        kimi_cli = await KimiCLI.create(
            session,
            mcp_configs=None,
            resumed=resumed,
            ui_mode="wire",
            thinking=session_thinking,
            agent_file=agent_file,
        )

    _DIAG["t_create_done"] = time.perf_counter()
    _emit_worker_timing(session_id)

    # Run in wire stdio mode
    await kimi_cli.run_wire_stdio()


# ── warm-pool (two-phase) entry ───────────────────────────────────────────────
# The gateway execs ``python -m kimi_cli.web.runner.worker --warm`` into a Pod
# that has no user yet. The worker does phase 1 (prepare_static), reports
# ``ready``, then blocks on stdin speaking ONLY the warm handshake protocol
# (web/runner/warm_protocol.py) until a bind frame claims it.

#: hard cap on one warm handshake line (bytes) — the frames are ~200B; a peer
#: that never sends a newline must not grow an unbounded buffer.
_WARM_MAX_LINE = 1 << 20


def _read_stdin_line_unbuffered(fd: int = 0) -> bytes | None:
    """Read exactly one ``\n``-terminated line from ``fd``, one byte at a time.

    🔴 Byte-at-a-time is not an oversight — it is the correctness requirement.
    After the handshake, ``WireServer`` builds its OWN reader over fd 0, so any
    byte this function reads past the trailing newline is lost forever. The
    gateway writes the client's ``initialize`` frame immediately after the bind
    frame, so a buffered reader would routinely swallow it into a buffer nobody
    ever drains — and the session would silently lose its capability handshake
    (exactly the AskUserQuestion class of bug). Warm handshake traffic is a
    handful of ~200-byte frames, so the syscall cost is irrelevant.

    Returns ``None`` at EOF (with no partial line), else the line without the
    trailing newline.
    """
    buf = bytearray()
    while True:
        chunk = os.read(fd, 1)
        if not chunk:
            return bytes(buf) if buf else None
        if chunk == b"\n":
            return bytes(buf)
        buf.extend(chunk)
        if len(buf) > _WARM_MAX_LINE:
            raise RuntimeError("warm handshake line exceeded size limit")


def _write_warm_frame(frame_type: str, **fields: Any) -> None:
    """Write one warm frame to the wire stdout (fd 1) and flush."""
    from kimi_cli.web.runner import warm_protocol  # noqa: PLC0415

    sys.stdout.write(warm_protocol.encode(frame_type, **fields))
    sys.stdout.flush()


async def _warm_handshake(prepared_at: float):
    """Serve the warm handshake until a valid bind frame arrives.

    Returns the parsed :class:`~kimi_cli.web.runner.warm_protocol.BindRequest`.
    Exits the process (never returns) on EOF or on an invalid bind — a Pod whose
    claim was malformed must not linger半绑定 in the pool; the gateway sees the
    error frame / exit code, marks the row dead and falls back to a cold start.
    """
    from kimi_cli.web.runner import warm_protocol as wp  # noqa: PLC0415

    while True:
        line = await asyncio.to_thread(_read_stdin_line_unbuffered)
        if line is None:
            logger.info("[warm] stdin closed before bind; exiting warm worker")
            sys.exit(0)
        frame = wp.decode(line)
        if frame is None:
            # Not a warm frame. The gateway must not send JSON-RPC before the
            # bind (it would be eaten as the bind line); log loudly and keep
            # waiting rather than mis-binding.
            logger.warning(
                "[warm] ignoring non-warm line received before bind: {line!r}",
                line=line[:200],
            )
            continue
        kind = frame.get(wp.WARM_KEY)
        if kind == wp.FRAME_PING:
            _write_warm_frame(
                wp.FRAME_PONG,
                seq=frame.get("seq"),
                uptime_ms=int((time.perf_counter() - prepared_at) * 1000),
            )
            continue
        if kind == wp.FRAME_BIND:
            try:
                return wp.BindRequest.parse(frame)
            except wp.WarmProtocolError as e:
                logger.error(
                    "[warm] bind rejected ({reason}): {detail}",
                    reason=e.reason,
                    detail=e.detail,
                )
                _write_warm_frame(wp.FRAME_ERROR, reason=e.reason, detail=e.detail)
                sys.exit(wp.WARM_BIND_FAILURE_EXIT_CODE)
        logger.warning("[warm] ignoring unexpected warm frame type {k!r}", k=kind)


async def run_warm_worker() -> None:
    """Two-phase worker entry: prepare, report ready, wait for a bind, then run."""
    from kimi_cli.web.runner import warm_protocol as wp  # noqa: PLC0415

    prepared = await prepare_static()
    _write_warm_frame(
        wp.FRAME_READY,
        ok=prepared.ok,
        reason=prepared.reason,
        agent=prepared.agent_name,
        agent_path=prepared.agent_path,
        pid=os.getpid(),
        prepare_ms=int(prepared.prepare_ms),
    )
    if not prepared.ok:
        logger.error(
            "[warm] static preparation failed ({reason}); exiting so the gateway "
            "discards this Pod instead of pooling a Pod that fails every claim.",
            reason=prepared.reason,
        )
        sys.exit(wp.WARM_PREPARE_FAILURE_EXIT_CODE)

    prepared_at = time.perf_counter()
    bind = await _warm_handshake(prepared_at)
    for key, value in bind.env.items():
        os.environ[key] = value
    _write_warm_frame(wp.FRAME_BOUND, session_id=str(bind.session_id))
    logger.info(
        "[warm] bound to session {sid} owner={owner} yolo={yolo}",
        sid=str(bind.session_id),
        owner=bind.owner_id,
        yolo=bind.yolo,
    )
    await bind_and_run(bind.session_id, owner_id=bind.owner_id, yolo=bind.yolo)


def _emit_agent_load_failure_to_wire(message: str, reason: str) -> None:
    """Write ONE JSON-RPC error frame to the wire stdout (fd 1) before exit.

    The worker dies before ``run_wire_stdio`` brings the WireServer up, and
    ``enable_logging`` has dup2'd fd 2 into ``kimi.log`` — so a bare traceback is
    invisible to the client. stdout (fd 1) is the wire channel the gateway reads
    line-by-line and broadcasts, so emitting a newline-terminated JSON-RPC error
    frame here is what actually surfaces the failure to the user. ``code`` reuses
    the dedicated exit code so the frame is self-describing; the gateway also
    detects the process exit code itself and re-broadcasts + records the metric.

    Best-effort: any failure to write must not mask the original error (the exit
    code is the gateway's authoritative signal regardless).
    """
    import contextlib  # noqa: PLC0415

    with contextlib.suppress(Exception):
        frame = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "agent-load-failure",
                "error": {
                    "code": AGENT_LOAD_FAILURE_EXIT_CODE,
                    "message": message,
                    "data": {"reason": reason},
                },
            },
            ensure_ascii=False,
        )
        sys.stdout.write(frame + "\n")
        sys.stdout.flush()


def main() -> None:
    """Entry point for the worker subprocess."""
    from kimi_cli.utils.proctitle import set_process_title
    from kimi_cli.utils.proxy import normalize_proxy_env

    normalize_proxy_env()
    set_process_title("kimi-code-worker")

    if len(sys.argv) < 2:
        print(
            "Usage: python -m kimi_cli.web.runner.worker <session_id> | --warm",
            file=sys.stderr,
        )
        sys.exit(1)

    # hechun-fork-cci (warm pool): ``--warm`` starts the two-phase worker — do the
    # session-independent preparation now, then block on the warm handshake until
    # the gateway binds a real session id / owner / yolo to this process.
    warm = sys.argv[1] == "--warm"
    session_id: UUID | None = None
    if not warm:
        try:
            session_id = UUID(sys.argv[1])
        except ValueError:
            print(f"Invalid session ID: {sys.argv[1]}", file=sys.stderr)
            sys.exit(1)

    # Enable logging for the subprocess
    enable_logging(debug=False)

    # hechun-fork-cci: enable_logging installs a FILE sink only (and dup2's fd 2
    # into it), so on CCI every runtime warning dies inside the Pod. Forward
    # WARNING+ to the gateway over the diag frame channel when
    # KIMO_WORKER_TRACE is on — see worker_diag for why this is gated, bounded
    # and non-recursive.
    from kimi_cli.web.runner.worker_diag import install_log_forwarding  # noqa: PLC0415

    install_log_forwarding(logger)

    # Run the async worker
    try:
        if warm:
            asyncio.run(run_warm_worker())
        else:
            assert session_id is not None
            asyncio.run(run_worker(session_id))
    except (AgentRequiredError, SubagentNotFoundError) as exc:
        # hechun-fork-cci: required agent failed to load. Surface a clear error
        # to the client over the wire stdout (the traceback alone goes to
        # kimi.log, invisible to the user — see _emit_agent_load_failure_to_wire)
        # and exit with the dedicated code so the gateway can record the metric +
        # re-broadcast a clean message.
        reason = getattr(exc, "reason", _REASON_AGENT_REQUIRED_MISSING)
        logger.error(
            "Agent load failed ({reason}); refusing to start: {err}",
            reason=reason,
            err=exc,
        )
        _emit_agent_load_failure_to_wire(str(exc), reason)
        sys.exit(AGENT_LOAD_FAILURE_EXIT_CODE)


if __name__ == "__main__":
    main()
