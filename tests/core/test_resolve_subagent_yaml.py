"""Tests for ``agentspec.resolve_subagent_yaml``.

The sandbox worker reads ``SUBAGENT`` from env and turns it into an
on-disk agent spec path via this helper.  Covers the four lookup layers
and the not-found path that the worker fail-fasts on.

See the spec at
``/Users/jie/Develop/hechun/app/docs/superpowers/specs/2026-06-02-ai-assistant-chat-design.md``
(§3.4).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kimi_cli.agentspec import resolve_subagent_yaml

# A minimal valid agent spec yaml body.  ``resolve_subagent_yaml`` only
# checks file existence — it does not parse the body — so a placeholder
# suffices.  We still write valid yaml so future stricter checks won't
# regress these tests.
_MIN_YAML = "version: 1\nagent:\n  name: {name}\n  system_prompt_path: ./system.md\n  tools: []\n"


@pytest.fixture
def fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``Path.home()`` at a tmpdir so each test gets a clean tree.

    ``resolve_subagent_yaml`` walks ``~/.config/agents/...`` and falls
    back to ``discover_user_agent_specs`` which also reads ``~/.kimi/``;
    isolating both via $HOME keeps tests hermetic on developer laptops
    that may have real agent specs installed.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    # ``Path.home()`` on POSIX honours $HOME; on Windows it goes through
    # ``USERPROFILE``.  We override both to make the test cross-platform.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _write_spec(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_MIN_YAML.format(name=name), encoding="utf-8")


def test_resolve_returns_none_when_nothing_matches(fake_home: Path, tmp_path: Path) -> None:
    work_dir = tmp_path / "wd"
    work_dir.mkdir()
    assert resolve_subagent_yaml("does-not-exist", work_dir=work_dir) is None


def test_resolve_finds_skill_nested_layout(fake_home: Path, tmp_path: Path) -> None:
    """Canonical hechun shape: ``~/.config/agents/skills/<bundle>/subagents/<name>.yaml``."""
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

    resolved = resolve_subagent_yaml("diabetes-expert", work_dir=tmp_path)
    assert resolved == target


def test_resolve_falls_back_to_flat_subagents_dir(fake_home: Path, tmp_path: Path) -> None:
    """Belt-and-suspenders: ``~/.config/agents/subagents/<name>.yaml``.

    Useful for bundles that don't follow the per-skill nesting (e.g. a
    user dropping a single yaml in by hand).
    """
    target = fake_home / ".config" / "agents" / "subagents" / "pump-coach.yaml"
    _write_spec(target, "pump-coach")

    resolved = resolve_subagent_yaml("pump-coach", work_dir=tmp_path)
    assert resolved == target


def test_resolve_skill_nested_layout_wins_over_flat(fake_home: Path, tmp_path: Path) -> None:
    """Both layouts present → the nested (skill-bundle) one wins.

    Rationale: skill-bundle nesting is the layout we ship and promote, so
    a deliberate bundle entry should always beat a stray flat file.
    """
    nested = (
        fake_home
        / ".config"
        / "agents"
        / "skills"
        / "hechun"
        / "subagents"
        / "diabetes-expert.yaml"
    )
    flat = fake_home / ".config" / "agents" / "subagents" / "diabetes-expert.yaml"
    _write_spec(nested, "diabetes-expert-nested")
    _write_spec(flat, "diabetes-expert-flat")

    resolved = resolve_subagent_yaml("diabetes-expert", work_dir=tmp_path)
    assert resolved == nested


def test_resolve_falls_back_to_discover_user_agent_specs(fake_home: Path, tmp_path: Path) -> None:
    """When neither config-tree path matches, the legacy ``~/.kimi/agents``
    layout (and project-local ``.kimi/agents``) is still honoured."""
    # Use a custom name so the spec_name resolution actually returns it.
    target = fake_home / ".kimi" / "agents" / "legacy-agent.yaml"
    _write_spec(target, "legacy-agent")

    resolved = resolve_subagent_yaml("legacy-agent", work_dir=tmp_path)
    assert resolved == target


def test_resolve_project_local_kimi_agents(fake_home: Path, tmp_path: Path) -> None:
    """Project-local ``<work_dir>/.kimi/agents/<name>.yaml`` is also picked up."""
    work_dir = tmp_path / "proj"
    work_dir.mkdir()
    target = work_dir / ".kimi" / "agents" / "myagent.yaml"
    _write_spec(target, "myagent")

    resolved = resolve_subagent_yaml("myagent", work_dir=work_dir)
    assert resolved == target
