"""MCP server config auto-discovery for the sandbox worker.

Sandboxes started by the gateway need to pick up MCP server configs
without forcing every caller to write into the kimi-cli global
``<share>/mcp.json``.  The hechun (avocado) integration in particular
ships its config under ``~/.config/agents/mcp.json`` (which the host
bind-mounts into the sandbox) so multiple kimo-using projects can each
declare their own servers without colliding.

This module reads those auto-discovered files, normalises their
``servers`` / ``mcpServers`` shape, substitutes ``${VAR}`` placeholders
against ``os.environ``, and hands the result back as a list of dicts
that the existing ``KimiCLI.create(mcp_configs=...)`` pipeline accepts.

Spec ref: ``2026-06-02-ai-assistant-chat-design.md`` (§3.2) under
``/Users/jie/Develop/hechun/app/docs/superpowers/specs/``.

Design notes
------------

* **Source paths** — we look at, in priority order:

  1. ``$XDG_CONFIG_HOME/agents/mcp.json`` (if XDG_CONFIG_HOME is set;
     overrides the default below)
  2. ``~/.config/agents/mcp.json``

  These are the same paths the hechun bundle README documents, so
  config-rendered-by-the-host gets picked up automatically.

* **Schema tolerance** — the hechun example JSON uses a ``servers``
  top-level key matching the MCP spec, while ``fastmcp`` validates
  against ``mcpServers``.  We accept either and normalise to
  ``mcpServers`` so existing code paths continue to work.  Comment
  fields (``_comment``) are silently dropped.

* **Env substitution** — we substitute placeholders in the string
  fields (``url``, header values).  Missing required env vars cause
  the offending server to be skipped with a warning, NOT a hard error
  — the rest of the config should still load (a worker that can't
  reach hechun MCP can still serve a default kimo session).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from kimi_cli import logger

# Same pattern as ``http_skill_runtime.substitute_env`` — kept local to
# avoid a cross-module dependency that would pull soul.* into the
# minimal worker bootstrap.
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def discover_mcp_config_files() -> list[Path]:
    """Return the auto-discovery candidate paths that exist on disk.

    Highest-priority entry first; callers that only want the "winning"
    config can take ``[0]``.  Currently used by the worker, which merges
    all of them so a host can layer e.g. a per-user override on top of
    the bundle default.
    """
    candidates: list[Path] = []
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        candidates.append(Path(xdg) / "agents" / "mcp.json")
    candidates.append(Path.home() / ".config" / "agents" / "mcp.json")

    # Dedupe while preserving order in case XDG_CONFIG_HOME == ``~/.config``.
    seen: set[Path] = set()
    out: list[Path] = []
    for c in candidates:
        try:
            resolved = c.resolve()
        except OSError:
            resolved = c
        if resolved in seen:
            continue
        seen.add(resolved)
        if c.is_file():
            out.append(c)
    return out


def _substitute_env_in_str(text: str, *, environ: dict[str, str]) -> tuple[str, list[str]]:
    """Expand placeholders, returning the substituted string + missing keys.

    Missing required (no-default) placeholders are accumulated rather
    than raising, so the caller can decide per-server whether to skip
    or hard-error.  This matches the "skip the broken bit, keep the
    rest" stance documented at the top of the module.
    """
    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        var = match.group(1)
        default = match.group(2)
        value = environ.get(var)
        if value is not None:
            return value
        if default is not None:
            return default
        missing.append(var)
        return ""

    return _ENV_VAR_PATTERN.sub(_replace, text), missing


def _substitute_in_value(value: Any, *, environ: dict[str, str], missing: list[str]) -> Any:
    """Walk a nested value, substituting env placeholders in strings only.

    Lists/dicts are walked recursively; other scalar types pass through
    unchanged.  Missing vars are appended to *missing* so the caller
    sees a consolidated list per server.
    """
    if isinstance(value, str):
        new, miss = _substitute_env_in_str(value, environ=environ)
        missing.extend(miss)
        return new
    if isinstance(value, list):
        return [_substitute_in_value(v, environ=environ, missing=missing) for v in value]
    if isinstance(value, dict):
        return {
            k: _substitute_in_value(v, environ=environ, missing=missing) for k, v in value.items()
        }
    return value


def _normalize_servers_key(raw: dict[str, Any]) -> dict[str, Any]:
    """Accept either ``servers`` or ``mcpServers`` at top level.

    The MCP spec writes ``servers``; ``fastmcp`` and our existing global
    config use ``mcpServers``.  We normalise to ``mcpServers`` so the
    downstream pipeline (``MCPConfig.model_validate``) doesn't care.
    """
    if "mcpServers" in raw:
        return raw
    if "servers" in raw:
        # Avoid mutating the caller's dict.
        out = {k: v for k, v in raw.items() if k != "servers"}
        out["mcpServers"] = raw["servers"]
        return out
    # No servers at all → return as-is; downstream validation will catch it.
    return raw


def load_mcp_config_file(
    path: Path, *, environ: dict[str, str] | None = None
) -> dict[str, Any] | None:
    """Parse one auto-discovered ``mcp.json`` file.

    Returns ``None`` when the file is missing, invalid JSON, or yields
    an empty config after dropping unresolvable servers.  Returns a
    dict with a ``mcpServers`` key suitable for ``MCPConfig.model_validate``
    otherwise.

    Servers whose ``${VAR}`` placeholders can't be resolved are dropped
    one-by-one with a warning; the rest of the config still loads.
    """
    env = environ if environ is not None else os.environ

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.info(
            "Skipping unreadable MCP config file {path}: {error}",
            path=path,
            error=exc,
        )
        return None

    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Invalid JSON in MCP config file {path}: {error}",
            path=path,
            error=exc,
        )
        return None
    if not isinstance(raw, dict):
        logger.warning(
            "Expected object at top of MCP config {path}, got {type}",
            path=path,
            type=type(raw).__name__,
        )
        return None

    normalized = _normalize_servers_key(raw)
    servers_in = normalized.get("mcpServers")
    if not isinstance(servers_in, dict) or not servers_in:
        # ``hechun.example.json`` carries an ``_comment`` array even
        # when no servers are present; treat that as "nothing to do"
        # rather than a hard error.
        return None

    servers_out: dict[str, Any] = {}
    for name, server_cfg in servers_in.items():
        if not isinstance(server_cfg, dict):
            logger.warning("Skipping non-dict MCP server {name} in {path}", name=name, path=path)
            continue
        missing: list[str] = []
        substituted = _substitute_in_value(server_cfg, environ=dict(env), missing=missing)
        if missing:
            logger.warning(
                "Skipping MCP server `{name}` from {path}: unresolved env var(s) {vars}",
                name=name,
                path=path,
                vars=sorted(set(missing)),
            )
            continue
        # Default transport to "http" for remote URLs without an explicit
        # transport (the hechun example does declare it; this is a safety
        # net for hand-edited configs).
        if "url" in substituted and "transport" not in substituted:
            substituted["transport"] = "http"
        servers_out[name] = substituted

    if not servers_out:
        return None
    return {"mcpServers": servers_out}


def load_auto_discovered_mcp_configs(
    *, environ: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """High-level entry: locate + parse all auto-discovered MCP configs.

    Empty list when nothing applies — the worker treats that as "no
    extra MCP" and proceeds normally.  Mostly a wrapper to keep the
    worker.py change to a single line.
    """
    out: list[dict[str, Any]] = []
    for path in discover_mcp_config_files():
        cfg = load_mcp_config_file(path, environ=environ)
        if cfg is not None:
            logger.info(
                "Loaded auto-discovered MCP config from {path}: {count} server(s)",
                path=path,
                count=len(cfg["mcpServers"]),
            )
            out.append(cfg)
    return out
