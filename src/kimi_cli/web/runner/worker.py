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
from pathlib import Path
from typing import Any
from uuid import UUID

from kimi_cli import logger
from kimi_cli.agentspec import resolve_subagent_yaml
from kimi_cli.app import KimiCLI, enable_logging
from kimi_cli.cli.mcp import get_global_mcp_config_file
from kimi_cli.exception import MCPConfigError
from kimi_cli.web.runner.mcp_discovery import load_auto_discovered_mcp_configs
from kimi_cli.web.store.sessions import load_session_by_id

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
        resp = httpx.get(url, headers=headers, timeout=30.0)
        resp.raise_for_status()
        data = resp.content
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
    except Exception as e:  # noqa: BLE001 — must not crash worker startup
        logger.error(
            "[sandbox-assets] failed to fetch sandbox assets from gateway url={url}: {err}. "
            "Custom agent config (e.g. SUBAGENT yaml) may be missing on this Pod.",
            url=url,
            err=e,
        )


async def run_worker(session_id: UUID) -> None:
    """Run the KimiCLI worker for a session."""
    # hechun-fork-cci: on the CCI path the Pod can't bind-mount the gateway
    # host, so pull static assets (~/.kimi/agents custom agent specs, etc.) from
    # the gateway and unpack them under $HOME BEFORE agent resolution below
    # consumes them. No-op on docker/local (env not injected). Run off-thread:
    # httpx.get is blocking and run_worker drives an asyncio event loop.
    await asyncio.to_thread(_fetch_sandbox_assets)

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

    # Run in wire stdio mode
    await kimi_cli.run_wire_stdio()


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
        print("Usage: python -m kimi_cli.web.runner.worker <session_id>", file=sys.stderr)
        sys.exit(1)

    try:
        session_id = UUID(sys.argv[1])
    except ValueError:
        print(f"Invalid session ID: {sys.argv[1]}", file=sys.stderr)
        sys.exit(1)

    # Enable logging for the subprocess
    enable_logging(debug=False)

    # Run the async worker
    try:
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
