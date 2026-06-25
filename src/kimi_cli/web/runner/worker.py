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


class SubagentNotFoundError(RuntimeError):
    """Raised when the sandbox is launched with a ``SUBAGENT`` env var that
    does not resolve to an agent spec yaml on disk.

    The sandbox worker treats this as fatal — silently falling back to the
    default agent would hide a misconfiguration (e.g. hechun broker injected
    ``SUBAGENT=diabetes-expert`` but the skill bundle wasn't mounted) and let
    the wrong system prompt + tool set serve real users.
    """


async def run_worker(session_id: UUID) -> None:
    """Run the KimiCLI worker for a session."""
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
        # owner_id 取自 gateway 转发的 KIMI_USER_ID，供 memory 归属（缺省走 sentinel）。
        owner_env = os.environ.get("KIMI_USER_ID") or None
        if owner_env:
            import contextlib  # noqa: PLC0415

            with contextlib.suppress(Exception):  # owner 设置失败不阻塞启动
                session.state.owner_id = owner_env

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
    asyncio.run(run_worker(session_id))


if __name__ == "__main__":
    main()
