"""Tests for the M1 hechun-integration session endpoints.

Covers:
- ``POST /api/sessions/{id}/stop`` (stop_session) — happy path + 404 + no-worker
- ``GET /api/sessions/{id}/memory_tail`` (get_session_memory_tail) — happy path,
  ``after_line`` paging, missing file fallback.

Both endpoints are M1 additions for the hechun (avocado) backend's
``SandboxBroker`` idle sweeper / ``MemorySyncJob``.  See
``custom-skills/hechun/README.md`` and the spec at
``/Users/jie/Develop/hechun/app/docs/superpowers/specs/2026-06-02-ai-assistant-chat-design.md``
(§3.3).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from kimi_cli.web.api import sessions as sessions_api

if TYPE_CHECKING:
    from kimi_cli.web.runner.process import KimiCLIRunner


# ---------------------------------------------------------------------------
# stop_session
# ---------------------------------------------------------------------------


class _StoppableSessionProcess:
    """Minimal stand-in for a SessionProcess that records ``stop()`` calls."""

    def __init__(self) -> None:
        self.stop_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1


class _RunnerWithSession:
    """Runner stub: returns a process when asked, mimicking active sandbox."""

    def __init__(self, process: _StoppableSessionProcess) -> None:
        self.process = process

    def get_session(self, _session_id: UUID) -> _StoppableSessionProcess:
        return self.process


class _RunnerWithoutSession:
    """Runner stub: no process for the given session id."""

    def get_session(self, _session_id: UUID) -> None:
        return None


class _FakeJointSession:
    """Lightweight stand-in for ``JointSession`` for endpoints that only need
    existence checks."""

    def __init__(self, session_id: UUID, *, owner_id: str | None = None) -> None:
        self.session_id = session_id
        self.owner_id = owner_id


@pytest.mark.anyio
async def test_stop_session_returns_404_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown session id surfaces as 404."""
    monkeypatch.setattr(sessions_api, "load_session_by_id", lambda _sid: None)

    with pytest.raises(HTTPException) as exc_info:
        await sessions_api.stop_session(
            uuid4(),
            runner=cast("KimiCLIRunner", _RunnerWithoutSession()),
        )

    assert exc_info.value.status_code == 404


@pytest.mark.anyio
async def test_stop_session_idempotent_when_no_active_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session exists but no worker → 200 with ``stopped=False`` (idempotent)."""
    sid = uuid4()
    monkeypatch.setattr(
        sessions_api,
        "load_session_by_id",
        lambda _sid: _FakeJointSession(sid),
    )

    response = await sessions_api.stop_session(
        sid,
        runner=cast("KimiCLIRunner", _RunnerWithoutSession()),
    )

    assert response.stopped is False
    assert response.session_id == sid
    assert response.detail is not None


@pytest.mark.anyio
async def test_stop_session_stops_active_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: active worker → ``stop()`` invoked, returns ``stopped=True``."""
    sid = uuid4()
    monkeypatch.setattr(
        sessions_api,
        "load_session_by_id",
        lambda _sid: _FakeJointSession(sid),
    )
    process = _StoppableSessionProcess()
    runner = _RunnerWithSession(process)

    response = await sessions_api.stop_session(
        sid,
        runner=cast("KimiCLIRunner", runner),
    )

    assert response.stopped is True
    assert response.session_id == sid
    assert process.stop_calls == 1


# ---------------------------------------------------------------------------
# get_session_memory_tail
# ---------------------------------------------------------------------------


def _write_persistent(share_dir: Path, owner_id: str, lines: list[str]) -> Path:
    """Write the canonical persistent.jsonl layout for tests."""
    target = share_dir / "users" / owner_id / "memory" / "persistent.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return target


@pytest.mark.anyio
async def test_memory_tail_returns_404_when_session_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sessions_api, "load_session_by_id", lambda _sid: None)

    with pytest.raises(HTTPException) as exc_info:
        await sessions_api.get_session_memory_tail(uuid4())

    assert exc_info.value.status_code == 404


@pytest.mark.anyio
async def test_memory_tail_returns_empty_when_file_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No persistent.jsonl on disk → empty lines, ``last_line_no=0``."""
    sid = uuid4()
    owner = "user-42"
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(
        sessions_api,
        "load_session_by_id",
        lambda _sid: _FakeJointSession(sid, owner_id=owner),
    )

    response = await sessions_api.get_session_memory_tail(sid)

    assert response.lines == []
    assert response.last_line_no == 0
    assert response.owner_id == owner


@pytest.mark.anyio
async def test_memory_tail_reads_full_file_when_after_line_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sid = uuid4()
    owner = "user-7"
    _write_persistent(
        tmp_path,
        owner,
        [
            '{"id": "a", "content": "first"}',
            '{"id": "b", "content": "second"}',
            '{"id": "c", "content": "third"}',
        ],
    )
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(
        sessions_api,
        "load_session_by_id",
        lambda _sid: _FakeJointSession(sid, owner_id=owner),
    )

    response = await sessions_api.get_session_memory_tail(sid, after_line=0)

    assert [line.line_no for line in response.lines] == [1, 2, 3]
    assert response.lines[0].content == '{"id": "a", "content": "first"}'
    assert response.last_line_no == 3
    assert response.owner_id == owner


@pytest.mark.anyio
async def test_memory_tail_paginates_by_after_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``after_line=N`` returns lines (N+1)..end (incremental pull pattern)."""
    sid = uuid4()
    owner = "user-9"
    _write_persistent(
        tmp_path,
        owner,
        [
            '{"id": "a"}',
            '{"id": "b"}',
            '{"id": "c"}',
            '{"id": "d"}',
        ],
    )
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(
        sessions_api,
        "load_session_by_id",
        lambda _sid: _FakeJointSession(sid, owner_id=owner),
    )

    response = await sessions_api.get_session_memory_tail(sid, after_line=2)

    assert [line.line_no for line in response.lines] == [3, 4]
    assert response.lines[0].content == '{"id": "c"}'
    assert response.last_line_no == 4


@pytest.mark.anyio
async def test_memory_tail_respects_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``limit`` caps the returned lines but ``last_line_no`` only advances
    as far as the file has been read so the caller can resume next call."""
    sid = uuid4()
    owner = "user-3"
    _write_persistent(
        tmp_path,
        owner,
        [f'{{"id": "{i}"}}' for i in range(1, 11)],
    )
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(
        sessions_api,
        "load_session_by_id",
        lambda _sid: _FakeJointSession(sid, owner_id=owner),
    )

    response = await sessions_api.get_session_memory_tail(sid, after_line=0, limit=3)

    assert [line.line_no for line in response.lines] == [1, 2, 3]
    assert response.last_line_no == 3  # caller can resume from here


@pytest.mark.anyio
async def test_memory_tail_falls_back_to_anonymous_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Session without owner_id reads from ``__anonymous__`` bucket."""
    from kimi_cli.memory.paths import ANONYMOUS_USER_SENTINEL

    sid = uuid4()
    _write_persistent(
        tmp_path,
        ANONYMOUS_USER_SENTINEL,
        ['{"id": "anon-1"}'],
    )
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
    monkeypatch.setattr(
        sessions_api,
        "load_session_by_id",
        lambda _sid: _FakeJointSession(sid, owner_id=None),
    )

    response = await sessions_api.get_session_memory_tail(sid)

    assert response.owner_id == ANONYMOUS_USER_SENTINEL
    assert len(response.lines) == 1
    assert response.lines[0].content == '{"id": "anon-1"}'


# ---------------------------------------------------------------------------
# create_session: ``subagent`` request param persists to session_config.json
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_share_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Reuse the fixture shape from test_sessions_api so we isolate share dir."""
    share_dir = tmp_path / "share"
    share_dir.mkdir()

    def _get_share_dir() -> Path:
        share_dir.mkdir(parents=True, exist_ok=True)
        return share_dir

    monkeypatch.setattr("kimi_cli.share.get_share_dir", _get_share_dir)
    monkeypatch.setattr("kimi_cli.metadata.get_share_dir", _get_share_dir)
    return share_dir


@pytest.mark.anyio
async def test_create_session_persists_subagent_to_session_config(
    isolated_share_dir: Path,
    tmp_path: Path,
) -> None:
    """``POST /api/sessions/`` with ``{"subagent": "diabetes-expert"}`` should
    end up in ``<session_dir>/session_config.json`` so the container runner
    can forward it as the ``SUBAGENT`` env var when starting the sandbox."""
    import json

    from starlette.requests import Request as StarletteRequest

    from kimi_cli.web.api.sessions import CreateSessionRequest, create_session

    work_dir = tmp_path / "wd"
    work_dir.mkdir()

    # Minimal ASGI scope so the FastAPI ``Request`` dependency works.
    scope = {
        "type": "http",
        "headers": [],
        "method": "POST",
        "path": "/api/sessions/",
        "query_string": b"",
        "app": cast(object, type("FakeApp", (), {"state": type("S", (), {})()})()),
    }
    http_request = StarletteRequest(scope)

    session = await create_session(
        http_request,
        request=CreateSessionRequest(
            work_dir=str(work_dir),
            subagent="diabetes-expert",
        ),
    )

    session_dir = Path(session.session_dir)
    config_path = session_dir / "session_config.json"
    assert config_path.is_file(), "session_config.json must be written"
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data.get("subagent") == "diabetes-expert"


@pytest.mark.anyio
async def test_create_session_without_subagent_omits_key(
    isolated_share_dir: Path,
    tmp_path: Path,
) -> None:
    """Backwards compatibility: omitting ``subagent`` must not write the key
    (sandbox runner treats absence as ‘use default agent’)."""
    import json

    from starlette.requests import Request as StarletteRequest

    from kimi_cli.web.api.sessions import CreateSessionRequest, create_session

    work_dir = tmp_path / "wd2"
    work_dir.mkdir()
    scope = {
        "type": "http",
        "headers": [],
        "method": "POST",
        "path": "/api/sessions/",
        "query_string": b"",
        "app": cast(object, type("FakeApp", (), {"state": type("S", (), {})()})()),
    }
    http_request = StarletteRequest(scope)

    session = await create_session(
        http_request,
        request=CreateSessionRequest(work_dir=str(work_dir)),
    )

    session_dir = Path(session.session_dir)
    config_path = session_dir / "session_config.json"
    # If file exists at all, it must not carry a stray empty ``subagent``.
    if config_path.is_file():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        assert "subagent" not in data
