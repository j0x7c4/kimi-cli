"""Integration tests for ``_register_http_skill_tools``.

Verifies that the HTTPSkillRuntime hook in ``soul.agent.load_agent``
actually populates ``KimiToolset`` from discovered skill directories,
and that the agent spec's skill allow/exclude filter is honoured.

Spec: ``/Users/jie/Develop/hechun/app/docs/superpowers/specs/2026-06-02-ai-assistant-chat-design.md``
(M1.5 (b)).
"""

from __future__ import annotations

from pathlib import Path

from kaos.path import KaosPath
from pydantic import BaseModel

from kimi_cli.agentspec import ResolvedAgentSpec
from kimi_cli.skill import Skill
from kimi_cli.soul.agent import Runtime, _register_http_skill_tools
from kimi_cli.soul.http_skill_runtime import HTTPSkillRuntime
from kimi_cli.soul.toolset import KimiToolset

_TOOL_YAML = """\
name: {name}
description: Test {name}
parameters:
  type: object
  properties: {{}}
runtime:
  type: http
  url: http://backend/api/v1/internal/ai/tool/{name}
  method: POST
  headers:
    Authorization: Bearer test
  body: $params
"""


def _make_skill(skill_dir: Path, name: str, *, with_tool_yaml: bool = True) -> Skill:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}", encoding="utf-8")
    if with_tool_yaml:
        (skill_dir / "tool.yaml").write_text(_TOOL_YAML.format(name=name), encoding="utf-8")
    kaos_dir = KaosPath.unsafe_from_local_path(skill_dir)
    kaos_md = KaosPath.unsafe_from_local_path(skill_dir / "SKILL.md")
    return Skill(
        name=name,
        description=f"Test {name}",
        type="standard",
        dir=kaos_dir,
        skill_md_file=kaos_md,
        scope="extra",
    )


class _StubRuntime(BaseModel):
    """Minimal Runtime stand-in.  ``_register_http_skill_tools`` only uses
    ``runtime.skills`` so we don't need the heavy fixture machinery."""

    model_config = {"arbitrary_types_allowed": True}

    skills: dict[str, Skill]


def _stub_spec(
    *,
    allowed_skills: list[str] | None = None,
    excluded_skills: list[str] | None = None,
) -> ResolvedAgentSpec:
    return ResolvedAgentSpec(
        name="test",
        system_prompt_path=Path("/tmp/x.md"),
        system_prompt_args={},
        model=None,
        when_to_use="",
        tools=[],
        allowed_tools=None,
        exclude_tools=[],
        subagents={},
        allowed_skills=allowed_skills,
        excluded_skills=excluded_skills or [],
    )


def test_registers_all_http_skills_when_no_filter(tmp_path: Path) -> None:
    """No allowlist → every discovered tool.yaml ends up as a tool."""
    skill_a = _make_skill(tmp_path / "a", "bolus_calc")
    skill_b = _make_skill(tmp_path / "b", "bg_interpret")
    toolset = KimiToolset()
    runtime = _StubRuntime(skills={"bolus_calc": skill_a, "bg_interpret": skill_b})

    _register_http_skill_tools(toolset, runtime, _stub_spec())  # type: ignore[arg-type]

    assert toolset.find("bolus_calc") is not None
    assert toolset.find("bg_interpret") is not None
    assert isinstance(toolset.find("bolus_calc"), HTTPSkillRuntime)


def test_allowlist_filters_out_other_skills(tmp_path: Path) -> None:
    """``allowed_skills=[bolus_calc]`` → only bolus_calc registered."""
    skill_a = _make_skill(tmp_path / "a", "bolus_calc")
    skill_b = _make_skill(tmp_path / "b", "bg_interpret")
    toolset = KimiToolset()
    runtime = _StubRuntime(skills={"bolus_calc": skill_a, "bg_interpret": skill_b})

    _register_http_skill_tools(
        toolset,
        runtime,
        _stub_spec(allowed_skills=["bolus_calc"]),  # type: ignore[arg-type]
    )

    assert toolset.find("bolus_calc") is not None
    assert toolset.find("bg_interpret") is None


def test_excludelist_drops_specific_skill(tmp_path: Path) -> None:
    skill_a = _make_skill(tmp_path / "a", "bolus_calc")
    skill_b = _make_skill(tmp_path / "b", "bg_interpret")
    toolset = KimiToolset()
    runtime = _StubRuntime(skills={"bolus_calc": skill_a, "bg_interpret": skill_b})

    _register_http_skill_tools(
        toolset,
        runtime,
        _stub_spec(excluded_skills=["bolus_calc"]),  # type: ignore[arg-type]
    )

    assert toolset.find("bolus_calc") is None
    assert toolset.find("bg_interpret") is not None


def test_skill_without_tool_yaml_is_ignored(tmp_path: Path) -> None:
    """Plain SKILL.md without a tool.yaml sibling stays as a regular skill
    (no callable tool registered)."""
    skill_a = _make_skill(tmp_path / "a", "bolus_calc")
    skill_no_yaml = _make_skill(tmp_path / "no", "just_a_doc", with_tool_yaml=False)
    toolset = KimiToolset()
    runtime = _StubRuntime(skills={"bolus_calc": skill_a, "just_a_doc": skill_no_yaml})

    _register_http_skill_tools(toolset, runtime, _stub_spec())  # type: ignore[arg-type]

    assert toolset.find("bolus_calc") is not None
    assert toolset.find("just_a_doc") is None


def test_does_not_clobber_existing_tool(tmp_path: Path) -> None:
    """If a built-in tool with the same name already exists, the HTTP
    skill registration must skip it with a warning rather than clobber."""
    from kosong.tooling import CallableTool, ToolOk

    class _Dummy(CallableTool):
        async def __call__(self, *args: object, **kw: object) -> object:  # pragma: no cover
            return ToolOk(output="")

    skill_a = _make_skill(tmp_path / "a", "bolus_calc")
    toolset = KimiToolset()
    builtin = _Dummy(name="bolus_calc", description="builtin", parameters={"type": "object"})
    toolset.add(builtin)
    runtime = _StubRuntime(skills={"bolus_calc": skill_a})

    _register_http_skill_tools(toolset, runtime, _stub_spec())  # type: ignore[arg-type]

    # Original builtin must still be there (not overwritten).
    assert toolset.find("bolus_calc") is builtin


def test_no_skills_is_noop() -> None:
    """Agent with no skills → no work, no errors."""
    toolset = KimiToolset()
    runtime = _StubRuntime(skills={})
    _register_http_skill_tools(toolset, runtime, _stub_spec())  # type: ignore[arg-type]
    assert toolset.tools == []


# Silence pytest collection warnings: ``runtime`` fixture name conflict.
# Re-export the Runtime type so the import is "used" for type hints in
# the docstring/intent above.
_ = Runtime  # noqa: F841
