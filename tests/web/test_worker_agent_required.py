"""Tests for the KIMI_REQUIRE_AGENT fail-fast guard in ``run_worker``.

# hechun-fork-cci

The gateway sets ``KIMI_REQUIRE_AGENT=1`` whenever it forwards a specific agent
name for a session (the session REQUIRES that agent). If agent resolution still
lands on the default agent, the worker must refuse to start rather than silently
serve the wrong agent — i.e. raise :class:`AgentRequiredError` BEFORE
``KimiCLI.create`` is ever called. When the env is unset the legacy
fall-back-to-default behaviour is preserved.

We drive the real ``run_worker`` with the heavy collaborators (assets fetch,
session create, MCP loading, KimiCLI.create) mocked, and assert specifically that
``KimiCLI.create`` is NOT reached when the guard fires.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from kimi_cli.web.runner import worker as worker_mod


class _FakeState:
    owner_id = None


class _FakeSessionDir:
    """Path-like stub: ``/ "x"`` returns a non-existent file stub."""

    def __truediv__(self, _other):
        return _FakeMissingFile()


class _FakeMissingFile:
    def exists(self) -> bool:
        return False

    def is_file(self) -> bool:
        return False


class _FakeSession:
    def __init__(self) -> None:
        self.state = _FakeState()
        self.dir = _FakeSessionDir()
        self.work_dir = "/app"


@pytest.fixture
def patched_worker(monkeypatch: pytest.MonkeyPatch):
    """Stub everything ``run_worker`` touches except the agent-resolution guard.

    Returns a dict with a ``create_called`` flag so tests can assert whether
    ``KimiCLI.create`` was reached.
    """
    calls = {"create_called": False, "run_wire_called": False}

    # No CCI asset download.
    monkeypatch.setattr(worker_mod, "_fetch_sandbox_assets", lambda: None)
    # CCI worker path: no session on disk → fresh-session branch.
    monkeypatch.setattr(worker_mod, "load_session_by_id", lambda sid: None)

    fake_session = _FakeSession()

    async def _fake_session_create(*args, **kwargs):
        return fake_session

    # Patch KimiCLISession.create (imported lazily inside run_worker).
    import kimi_cli.session as session_mod

    monkeypatch.setattr(session_mod.Session, "create", staticmethod(_fake_session_create))

    # No MCP config files.
    class _NoFile:
        def exists(self) -> bool:
            return False

    monkeypatch.setattr(worker_mod, "get_global_mcp_config_file", lambda: _NoFile())
    monkeypatch.setattr(worker_mod, "load_auto_discovered_mcp_configs", lambda: [])

    # No subagent yaml ever resolves (forces agent_file None unless config set).
    monkeypatch.setattr(worker_mod, "resolve_subagent_yaml", lambda name, work_dir=None: None)

    class _FakeKimiCLI:
        @staticmethod
        async def create(*args, **kwargs):
            calls["create_called"] = True
            instance = _FakeKimiCLI()
            return instance

        async def run_wire_stdio(self):
            calls["run_wire_called"] = True

    monkeypatch.setattr(worker_mod, "KimiCLI", _FakeKimiCLI)

    # Clean slate for the guard env.
    monkeypatch.delenv("KIMI_REQUIRE_AGENT", raising=False)
    monkeypatch.delenv("SUBAGENT", raising=False)
    monkeypatch.setenv("KIMI_WORK_DIR", "/app")

    return calls


async def test_require_agent_with_no_agent_raises_before_create(
    patched_worker, monkeypatch: pytest.MonkeyPatch
):
    """KIMI_REQUIRE_AGENT=1 + no resolved agent → AgentRequiredError, no create."""
    monkeypatch.setenv("KIMI_REQUIRE_AGENT", "1")

    with pytest.raises(worker_mod.AgentRequiredError):
        await worker_mod.run_worker(uuid4())

    assert patched_worker["create_called"] is False
    assert patched_worker["run_wire_called"] is False


async def test_no_require_agent_keeps_legacy_default_path(
    patched_worker, monkeypatch: pytest.MonkeyPatch
):
    """Env unset → legacy behaviour: falls back to default agent, create runs."""
    # KIMI_REQUIRE_AGENT deliberately unset by the fixture.
    await worker_mod.run_worker(uuid4())

    assert patched_worker["create_called"] is True
    assert patched_worker["run_wire_called"] is True


async def test_subagent_set_but_unresolved_still_fails_fast(
    patched_worker, monkeypatch: pytest.MonkeyPatch
):
    """SUBAGENT set but unresolvable → SubagentNotFoundError (existing guard)."""
    monkeypatch.setenv("SUBAGENT", "diabetes-expert")
    # Even without KIMI_REQUIRE_AGENT, an explicit unresolved SUBAGENT is fatal.

    with pytest.raises(worker_mod.SubagentNotFoundError):
        await worker_mod.run_worker(uuid4())

    assert patched_worker["create_called"] is False


class TestExceptionReasonLabels:
    def test_agent_required_error_reason(self):
        assert worker_mod.AgentRequiredError.reason == "agent_required_missing"

    def test_subagent_not_found_reason(self):
        assert worker_mod.SubagentNotFoundError.reason == "subagent_unresolved"

    def test_exit_code_is_42(self):
        assert worker_mod.AGENT_LOAD_FAILURE_EXIT_CODE == 42


class TestWireErrorEmission:
    def test_emits_jsonrpc_error_frame_to_stdout(self, capsys):
        """The pre-exit wire emitter writes a single JSON-RPC error line to
        stdout (the channel the gateway broadcasts to the client)."""
        import json

        worker_mod._emit_agent_load_failure_to_wire(
            "required agent could not be loaded", "agent_required_missing"
        )
        out = capsys.readouterr().out.strip()
        assert out, "expected a wire error frame on stdout"
        frame = json.loads(out)
        assert frame["error"]["code"] == worker_mod.AGENT_LOAD_FAILURE_EXIT_CODE
        assert "required agent" in frame["error"]["message"]
        assert frame["error"]["data"]["reason"] == "agent_required_missing"
