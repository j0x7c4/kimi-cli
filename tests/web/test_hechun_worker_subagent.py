"""Tests for the M1.5 sandbox-worker ``SUBAGENT`` env loader.

The web runner forwards a per-session ``SUBAGENT`` env var into the
sandbox; this test pins the worker contract that consumes it:

- env unset → no override (legacy default-agent path stays intact)
- env set + bundle mounted → ``agent_file`` resolves to the matching yaml
- env set + bundle missing → fail-fast with ``SubagentNotFoundError``

We exercise the resolution path that the worker uses (``run_worker``'s
SUBAGENT block) rather than spinning up ``KimiCLI.create`` itself, which
would need a real chat provider and llm.  The block is a thin wrapper
around ``resolve_subagent_yaml``; the integration boundary we care about
is "does the env var actually reach an agent_file decision?".

Spec ref: ``/Users/jie/Develop/hechun/app/docs/superpowers/specs/2026-06-02-ai-assistant-chat-design.md``
(§3.3 / §3.4).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kimi_cli.web.runner import worker as worker_mod

_MIN_YAML = "version: 1\nagent:\n  name: {name}\n  system_prompt_path: ./system.md\n  tools: []\n"


def _write_spec(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_MIN_YAML.format(name=name), encoding="utf-8")


@pytest.fixture
def fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate ``Path.home()`` so test results don't leak into the dev's
    real ``~/.config/agents`` or ``~/.kimi`` directories."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _resolve_via_worker_logic(subagent_env_value: str | None, *, work_dir: Path) -> Path | None:
    """Replicate the SUBAGENT branch in ``run_worker`` without invoking the
    full async setup (which needs DB + LLM).  The branch is small enough
    that mirroring it here keeps the test focused on the contract.
    """

    from kimi_cli.agentspec import resolve_subagent_yaml

    if subagent_env_value is None:
        # Simulate the env var being absent altogether (the most common
        # legacy path).
        return None
    subagent_name = subagent_env_value.strip()
    if not subagent_name:
        # Whitespace-only counts as unset, matching the env-precedence
        # behaviour the worker uses (``os.environ.get(...).strip()``).
        return None
    path = resolve_subagent_yaml(subagent_name, work_dir=work_dir)
    if path is None:
        raise worker_mod.SubagentNotFoundError(
            f"SUBAGENT={subagent_name!r} did not resolve to an agent spec"
        )
    return path


def test_unset_subagent_yields_no_override(fake_home: Path, tmp_path: Path) -> None:
    """No env → no agent_file override.

    Backwards-compat check: stand-alone ``kimi-cli`` users (no hechun
    skill bundle mounted) must keep getting the default agent.
    """
    assert _resolve_via_worker_logic(None, work_dir=tmp_path) is None


def test_blank_subagent_yields_no_override(fake_home: Path, tmp_path: Path) -> None:
    """Empty / whitespace env → no override, same as unset."""
    assert _resolve_via_worker_logic("", work_dir=tmp_path) is None
    assert _resolve_via_worker_logic("   ", work_dir=tmp_path) is None


def test_subagent_resolves_to_skill_bundle_yaml(fake_home: Path, tmp_path: Path) -> None:
    """Happy path: hechun bundle mounted → SUBAGENT picks the right yaml."""
    target = (
        fake_home
        / ".config"
        / "agents"
        / "skills"
        / "hechun"
        / "subagents"
        / "diabetes-expert.yaml"
    )
    _write_spec(target, "diabetes-expert")

    resolved = _resolve_via_worker_logic("diabetes-expert", work_dir=tmp_path)
    assert resolved == target


def test_subagent_missing_raises_fail_fast(fake_home: Path, tmp_path: Path) -> None:
    """SUBAGENT set + bundle not mounted → fail-fast.

    Silently falling back to the default agent would let the wrong system
    prompt + tool set serve real users; the broker explicitly opted in to
    a specific subagent, so absence is a configuration error.
    """
    with pytest.raises(worker_mod.SubagentNotFoundError):
        _resolve_via_worker_logic("diabetes-expert", work_dir=tmp_path)
