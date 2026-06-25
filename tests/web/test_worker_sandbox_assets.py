"""Tests for the CCI worker's sandbox-assets download + extract step.

# hechun-fork-cci

The worker (running inside a CCI Pod with no bind-mount) fetches a tar of
static assets from the gateway and unpacks it under ``$HOME`` before agent
resolution. Covered here:

- env set → httpx GET (with Bearer token) → tar extracted into HOME, files land.
- env unset → no download attempted (docker/local path untouched).
- tar member with ``..`` / absolute path → rejected, not written outside HOME.
- download failure → logged, does NOT raise (worker continues to the
  fail-fast SubagentNotFoundError path with a clear breadcrumb).
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from kimi_cli.web.runner import worker as worker_mod


def _make_tar(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class _FakeResp:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:  # pragma: no cover - trivial
        pass


def test_fetch_extracts_bundle_into_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("KIMO_SANDBOX_ASSETS_URL", "http://gw/internal/sandbox-assets/bundle.tar")
    monkeypatch.setenv("KIMO_SANDBOX_ASSETS_TOKEN", "tok-xyz")

    tar = _make_tar(
        {".kimi/agents/diabetes-expert.yaml": b"version: 1\n"}
    )

    captured: dict[str, object] = {}

    def fake_get(url: str, *, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResp(tar)

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)

    worker_mod._fetch_sandbox_assets()

    # Token forwarded as Bearer.
    assert captured["headers"] == {"Authorization": "Bearer tok-xyz"}
    # File restored under the fake HOME.
    assert (tmp_path / ".kimi" / "agents" / "diabetes-expert.yaml").is_file()


def test_fetch_noop_when_url_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KIMO_SANDBOX_ASSETS_URL", raising=False)

    called = False

    def fake_get(*a, **k):  # pragma: no cover - must not be reached
        nonlocal called
        called = True
        raise AssertionError("httpx.get should not be called when URL unset")

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)

    worker_mod._fetch_sandbox_assets()
    assert called is False


def test_fetch_rejects_path_traversal_members(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside.txt"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("KIMO_SANDBOX_ASSETS_URL", "http://gw/bundle.tar")

    tar = _make_tar(
        {
            "../outside.txt": b"evil\n",
            "/abs/evil.txt": b"evil\n",
            ".kimi/agents/ok.yaml": b"ok\n",
        }
    )

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(tar))

    worker_mod._fetch_sandbox_assets()

    # Traversal members must not have escaped HOME.
    assert not outside.exists()
    # Safe member still extracted.
    assert (home / ".kimi" / "agents" / "ok.yaml").is_file()


def test_fetch_download_failure_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KIMO_SANDBOX_ASSETS_URL", "http://gw/bundle.tar")

    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", boom)

    # Must NOT raise — failure is logged, worker continues.
    worker_mod._fetch_sandbox_assets()


def test_is_safe_tar_member() -> None:
    assert worker_mod._is_safe_tar_member(".kimi/agents/x.yaml") is True
    assert worker_mod._is_safe_tar_member("../x") is False
    assert worker_mod._is_safe_tar_member("/abs") is False
    assert worker_mod._is_safe_tar_member("a/../../b") is False
    assert worker_mod._is_safe_tar_member("") is False
