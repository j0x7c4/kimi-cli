"""Tests for ``web.runner.mcp_discovery``.

The sandbox worker auto-discovers MCP server configs from
``~/.config/agents/mcp.json`` (the hechun bundle convention).  This
module covers:

- file discovery (XDG_CONFIG_HOME, missing file, $HOME fallback)
- the ``servers`` / ``mcpServers`` key normalisation (the MCP spec
  writes ``servers``; fastmcp wants ``mcpServers``)
- env var substitution against ``os.environ``
- robust degradation: bad JSON, unresolved ${VAR}s, non-dict bodies all
  result in "skip with a warning" not "crash the worker"

Spec ref: ``2026-06-02-ai-assistant-chat-design.md`` (§3.2) under
``/Users/jie/Develop/hechun/app/docs/superpowers/specs/``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kimi_cli.web.runner.mcp_discovery import (
    discover_mcp_config_files,
    load_auto_discovered_mcp_configs,
    load_mcp_config_file,
)

# Minimal valid example mirroring ``custom-skills/hechun/mcp/hechun.example.json``.
_HECHUN_EXAMPLE = {
    "_comment": "ignore me",
    "servers": {
        "hechun": {
            "transport": "http",
            "url": "${HECHUN_MCP_URL}",
            "headers": {"Authorization": "Bearer ${HECHUN_MCP_TOKEN}"},
        }
    },
}


# ---------------------------------------------------------------------------
# discover_mcp_config_files
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``Path.home()`` at a tmpdir.

    Same trick as the resolve-subagent test: avoids cross-test pollution
    if the dev has a real ``~/.config/agents/mcp.json`` installed."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # Don't let the dev's XDG_CONFIG_HOME bleed in either.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


def test_discover_returns_empty_when_no_files(fake_home: Path) -> None:
    assert discover_mcp_config_files() == []


def test_discover_returns_default_path_when_present(fake_home: Path) -> None:
    target = fake_home / ".config" / "agents" / "mcp.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")

    assert discover_mcp_config_files() == [target]


def test_discover_respects_xdg_config_home_override(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When XDG_CONFIG_HOME is set, that path comes first."""
    xdg_dir = tmp_path / "alt"
    xdg_dir.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_dir))

    xdg_file = xdg_dir / "agents" / "mcp.json"
    xdg_file.parent.mkdir()
    xdg_file.write_text("{}", encoding="utf-8")
    home_file = fake_home / ".config" / "agents" / "mcp.json"
    home_file.parent.mkdir(parents=True)
    home_file.write_text("{}", encoding="utf-8")

    files = discover_mcp_config_files()
    # XDG entry first (higher precedence).
    assert files[0] == xdg_file
    assert home_file in files


# ---------------------------------------------------------------------------
# load_mcp_config_file
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_normalises_servers_to_mcpservers(tmp_path: Path) -> None:
    """The MCP spec writes ``servers``; we accept it and rename to
    ``mcpServers`` so fastmcp's validator is happy downstream."""
    f = _write_json(tmp_path / "mcp.json", _HECHUN_EXAMPLE)
    env = {
        "HECHUN_MCP_URL": "https://backend/mcp",
        "HECHUN_MCP_TOKEN": "tok-123",
    }
    cfg = load_mcp_config_file(f, environ=env)
    assert cfg is not None
    assert "mcpServers" in cfg
    assert "servers" not in cfg
    assert cfg["mcpServers"]["hechun"]["url"] == "https://backend/mcp"
    auth = cfg["mcpServers"]["hechun"]["headers"]["Authorization"]
    assert auth == "Bearer tok-123"


def test_load_passes_through_mcpservers_key(tmp_path: Path) -> None:
    """A file that already uses ``mcpServers`` is unchanged."""
    payload = {"mcpServers": {"echo": {"url": "http://example.org", "transport": "http"}}}
    f = _write_json(tmp_path / "mcp.json", payload)
    cfg = load_mcp_config_file(f, environ={})
    assert cfg is not None
    assert list(cfg["mcpServers"].keys()) == ["echo"]


def test_load_skips_server_with_unresolved_env(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Unresolved ${VAR}s drop the offending server, keeping the rest."""
    payload = {
        "servers": {
            "hechun": {
                "transport": "http",
                "url": "${HECHUN_MCP_URL}",  # unset
            },
            "echo": {"url": "http://localhost", "transport": "http"},
        }
    }
    f = _write_json(tmp_path / "mcp.json", payload)
    cfg = load_mcp_config_file(f, environ={})
    assert cfg is not None
    assert list(cfg["mcpServers"].keys()) == ["echo"]


def test_load_handles_default_placeholder(tmp_path: Path) -> None:
    """``${VAR:-default}`` uses the default when VAR is unset."""
    payload = {"servers": {"x": {"url": "${MISSING:-http://fallback}", "transport": "http"}}}
    f = _write_json(tmp_path / "mcp.json", payload)
    cfg = load_mcp_config_file(f, environ={})
    assert cfg is not None
    assert cfg["mcpServers"]["x"]["url"] == "http://fallback"


def test_load_returns_none_for_invalid_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed JSON → ``None`` + warning, never an exception."""
    f = tmp_path / "mcp.json"
    f.write_text("{this is: not json", encoding="utf-8")
    assert load_mcp_config_file(f, environ={}) is None


def test_load_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert load_mcp_config_file(tmp_path / "absent.json", environ={}) is None


def test_load_returns_none_for_empty_servers(tmp_path: Path) -> None:
    """``{"servers": {}}`` is "nothing to do", not a hard error."""
    f = _write_json(tmp_path / "mcp.json", {"servers": {}})
    assert load_mcp_config_file(f, environ={}) is None


def test_load_drops_comment_only_payload(tmp_path: Path) -> None:
    """A comment-only file (the example.json shape) yields None."""
    f = _write_json(tmp_path / "mcp.json", {"_comment": ["help me"]})
    assert load_mcp_config_file(f, environ={}) is None


def test_load_supplies_default_transport_when_missing(tmp_path: Path) -> None:
    """If a server has a ``url`` but no ``transport``, default to ``http``."""
    payload = {"servers": {"plain": {"url": "http://x"}}}
    f = _write_json(tmp_path / "mcp.json", payload)
    cfg = load_mcp_config_file(f, environ={})
    assert cfg is not None
    assert cfg["mcpServers"]["plain"]["transport"] == "http"


# ---------------------------------------------------------------------------
# load_auto_discovered_mcp_configs (end-to-end wrapper)
# ---------------------------------------------------------------------------


def test_auto_discover_returns_empty_when_no_files(fake_home: Path) -> None:
    assert load_auto_discovered_mcp_configs() == []


def test_auto_discover_picks_up_home_file(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HECHUN_MCP_URL", "https://backend/mcp")
    monkeypatch.setenv("HECHUN_MCP_TOKEN", "tok-xyz")

    target = fake_home / ".config" / "agents" / "mcp.json"
    _write_json(target, _HECHUN_EXAMPLE)

    configs = load_auto_discovered_mcp_configs()
    assert len(configs) == 1
    assert "hechun" in configs[0]["mcpServers"]
    assert configs[0]["mcpServers"]["hechun"]["url"] == "https://backend/mcp"
