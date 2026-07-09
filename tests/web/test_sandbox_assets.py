"""Tests for the internal sandbox-assets bundle endpoint + the env contract.

# hechun-fork-cci

CCI Pod workers can't bind-mount the gateway host, so they download a tar of
``$HOME``-relative static dirs (``~/.kimi/agents`` today) from this endpoint and
unpack it locally. Covered here:

- ``build_assets_tar`` packs the agents dir under a ``$HOME``-relative arcname,
  and skips a missing dir (empty tar, no error).
- the endpoint serves 200 with a valid Bearer token / matching ``?token=``, and
  401 with a wrong/absent token when a session token is configured; open when
  no session token is configured (dev mode).
- ``cci_process._build_sandbox_env`` injects ``KIMO_SANDBOX_ASSETS_URL`` +
  ``KIMO_SANDBOX_ASSETS_TOKEN`` only when ``KIMO_GATEWAY_INTERNAL_URL`` is set.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kimi_cli.web.api.sandbox_assets import (
    SANDBOX_ASSETS_PATH,
    build_assets_tar,
    router,
)


def _write_agent(home: Path) -> Path:
    agents = home / ".kimi" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    spec = agents / "diabetes-expert.yaml"
    spec.write_text("version: 1\nagent:\n  name: diabetes-expert\n", encoding="utf-8")
    (agents / "diabetes-system.md").write_text("# system prompt\n", encoding="utf-8")
    return spec


def _write_mcp_config(home: Path) -> Path:
    cfg_dir = home / ".config" / "agents"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    mcp = cfg_dir / "mcp.json"
    mcp.write_text(
        '{"servers":{"hechun":{"url":"${HECHUN_MCP_URL}",'
        '"headers":{"Authorization":"Bearer ${HECHUN_MCP_TOKEN}",'
        '"X-Hechun-User":"${KIMI_USER_ID}"}}}}',
        encoding="utf-8",
    )
    return mcp


def _tar_member_names(data: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        return tar.getnames()


# ── build_assets_tar ────────────────────────────────────────────────────────


def test_build_tar_packs_agents_dir_relative_to_home(tmp_path: Path) -> None:
    _write_agent(tmp_path)
    data = build_assets_tar(tmp_path)
    names = _tar_member_names(data)
    assert ".kimi/agents/diabetes-expert.yaml" in names
    assert ".kimi/agents/diabetes-system.md" in names
    # No absolute paths leaked into the tar.
    assert all(not n.startswith("/") for n in names)


def test_build_tar_extracts_back_to_home(tmp_path: Path) -> None:
    """Round-trip: pack from one HOME, unpack into another → files restored."""
    src = tmp_path / "src"
    src.mkdir()
    _write_agent(src)
    data = build_assets_tar(src)

    dst = tmp_path / "dst"
    dst.mkdir()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        tar.extractall(path=dst)  # noqa: S202 — trusted test fixture
    assert (dst / ".kimi" / "agents" / "diabetes-expert.yaml").is_file()


def test_build_tar_packs_mcp_config_under_config_agents(tmp_path: Path) -> None:
    """mcp.json under ~/.config/agents is packed relative to $HOME so the worker
    unpacks it to ~/.config/agents/mcp.json (the auto-discovery path)."""
    _write_mcp_config(tmp_path)
    data = build_assets_tar(tmp_path)
    names = _tar_member_names(data)
    assert ".config/agents/mcp.json" in names
    assert all(not n.startswith("/") for n in names)


def test_build_tar_missing_dir_yields_empty_tar(tmp_path: Path) -> None:
    """No ~/.kimi/agents → empty tar, not an error (fresh deploy)."""
    data = build_assets_tar(tmp_path)
    assert _tar_member_names(data) == []


# ── endpoint auth ───────────────────────────────────────────────────────────


def _make_app(session_token: str | None, home: Path) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.session_token = session_token

    # Patch Path.home() inside the handler via dependency on build_assets_tar;
    # easiest is to monkeypatch at call sites, but the handler reads Path.home()
    # directly, so the test monkeypatches HOME via fixture before TestClient use.
    return app


def test_endpoint_requires_token_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _write_agent(tmp_path)

    app = _make_app(session_token="s3cr3t", home=tmp_path)
    client = TestClient(app)

    # No token → 401
    resp = client.get(SANDBOX_ASSETS_PATH)
    assert resp.status_code == 401

    # Wrong token → 401
    resp = client.get(
        SANDBOX_ASSETS_PATH, headers={"Authorization": "Bearer nope"}
    )
    assert resp.status_code == 401

    # Correct Bearer token → 200 + tar
    resp = client.get(
        SANDBOX_ASSETS_PATH, headers={"Authorization": "Bearer s3cr3t"}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-tar"
    assert ".kimi/agents/diabetes-expert.yaml" in _tar_member_names(resp.content)


def test_endpoint_accepts_query_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _write_agent(tmp_path)

    app = _make_app(session_token="s3cr3t", home=tmp_path)
    client = TestClient(app)

    resp = client.get(SANDBOX_ASSETS_PATH, params={"token": "s3cr3t"})
    assert resp.status_code == 200


def test_endpoint_open_when_no_session_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Dev mode (no token) → endpoint serves without auth, like the rest of the API."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _write_agent(tmp_path)

    app = _make_app(session_token=None, home=tmp_path)
    client = TestClient(app)

    resp = client.get(SANDBOX_ASSETS_PATH)
    assert resp.status_code == 200


# ── env injection (cci_process._build_sandbox_env) ──────────────────────────


def _build_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    from kimi_cli.web.runner.cci_process import CCISessionProcess

    sid = uuid4()
    proc = CCISessionProcess(sid, spawner=SimpleNamespace())  # type: ignore[arg-type]
    return proc._build_sandbox_env()


def test_env_injects_assets_url_and_token_when_internal_url_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
    monkeypatch.setenv("KIMO_GATEWAY_INTERNAL_URL", "http://10.0.0.5:5494")
    monkeypatch.setenv("KIMI_WEB_SESSION_TOKEN", "tok-123")

    env = _build_env(monkeypatch)
    assert env["KIMO_SANDBOX_ASSETS_URL"] == f"http://10.0.0.5:5494{SANDBOX_ASSETS_PATH}"
    assert env["KIMO_SANDBOX_ASSETS_TOKEN"] == "tok-123"


def test_env_strips_trailing_slash_on_internal_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
    monkeypatch.setenv("KIMO_GATEWAY_INTERNAL_URL", "http://10.0.0.5:5494/")
    env = _build_env(monkeypatch)
    assert env["KIMO_SANDBOX_ASSETS_URL"] == f"http://10.0.0.5:5494{SANDBOX_ASSETS_PATH}"


def test_env_omits_assets_when_internal_url_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
    monkeypatch.delenv("KIMO_GATEWAY_INTERNAL_URL", raising=False)
    monkeypatch.setenv("KIMI_WEB_SESSION_TOKEN", "tok-123")

    env = _build_env(monkeypatch)
    assert "KIMO_SANDBOX_ASSETS_URL" not in env
    assert "KIMO_SANDBOX_ASSETS_TOKEN" not in env
