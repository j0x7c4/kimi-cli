"""Tests for the hechun-integration env transparency in ContainerSessionProcess.

Covers:
- ``_read_subagent_from_disk`` returns the persisted subagent or None.
- ``_build_docker_cmd`` includes ``SUBAGENT=...`` env when persisted.
- ``_build_docker_cmd`` forwards the hechun env vars from the process env.

These are M1 (kimo) changes for the hechun (avocado) AI assistant chat.
See ``custom-skills/hechun/README.md`` and the spec at
``/Users/jie/Develop/hechun/app/docs/superpowers/specs/2026-06-02-ai-assistant-chat-design.md``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from kimi_cli.web.runner import container as container_mod


def _write_session_config(
    share_dir: Path,
    session_id: str,
    payload: dict[str, str],
) -> Path:
    """Drop a session_config.json at the canonical
    ``<share>/sessions/<hash>/<sid>/session_config.json`` path."""
    import json

    session_dir = share_dir / "sessions" / "deadbeef" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    cfg = session_dir / "session_config.json"
    cfg.write_text(json.dumps(payload), encoding="utf-8")
    return cfg


def test_read_subagent_returns_none_when_share_dir_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KIMI_SHARE_DIR", raising=False)
    assert container_mod._read_subagent_from_disk(uuid4()) is None


def test_read_subagent_returns_none_when_config_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
    assert container_mod._read_subagent_from_disk(uuid4()) is None


def test_read_subagent_returns_persisted_value(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sid = uuid4()
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
    _write_session_config(tmp_path, str(sid), {"subagent": "diabetes-expert"})
    assert container_mod._read_subagent_from_disk(sid) == "diabetes-expert"


def test_read_subagent_ignores_blank_string(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sid = uuid4()
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
    _write_session_config(tmp_path, str(sid), {"subagent": "   "})
    assert container_mod._read_subagent_from_disk(sid) is None


def test_read_subagent_ignores_non_string(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sid = uuid4()
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
    # ``subagent`` deliberately non-string to ensure we never coerce.
    _write_session_config(tmp_path, str(sid), {"subagent": ""})  # type: ignore[arg-type]
    assert container_mod._read_subagent_from_disk(sid) is None


def test_build_docker_cmd_includes_subagent_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End-to-end: persisted ``subagent`` reaches the ``docker run -e`` args."""
    sid = uuid4()
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
    _write_session_config(tmp_path, str(sid), {"subagent": "diabetes-expert"})

    proc = container_mod.ContainerSessionProcess(sid)
    cmd = proc._build_docker_cmd()
    assert "SUBAGENT=diabetes-expert" in cmd


def test_build_docker_cmd_omits_subagent_when_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No ``subagent`` persisted → no SUBAGENT env in the docker cmd."""
    sid = uuid4()
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
    proc = container_mod.ContainerSessionProcess(sid)
    cmd = proc._build_docker_cmd()
    assert not any(part.startswith("SUBAGENT=") for part in cmd)


def test_build_docker_cmd_forwards_hechun_env_vars(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Hechun integration env vars on the gateway are passed into the sandbox."""
    sid = uuid4()
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
    monkeypatch.setenv("HECHUN_INTERNAL_BASE_URL", "http://backend:8081")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "secret-token-xyz")
    monkeypatch.setenv("HECHUN_MCP_URL", "http://backend:8081/mcp")
    monkeypatch.setenv("HECHUN_MCP_TOKEN", "mcp-jwt-xyz")

    proc = container_mod.ContainerSessionProcess(sid)
    cmd = proc._build_docker_cmd()

    assert "HECHUN_INTERNAL_BASE_URL=http://backend:8081" in cmd
    assert "INTERNAL_API_TOKEN=secret-token-xyz" in cmd
    assert "HECHUN_MCP_URL=http://backend:8081/mcp" in cmd
    assert "HECHUN_MCP_TOKEN=mcp-jwt-xyz" in cmd
