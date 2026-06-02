"""Tests for ``soul.http_skill_runtime``.

Covers:

- yaml parsing (happy path + error cases)
- env-var substitution (``${VAR}`` / ``${VAR:-default}`` / missing var)
- end-to-end call flow with mocked httpx (happy / 4xx / 5xx retry /
  transport retry)
- discovery scan over a tree of skill dirs

Spec: ``/Users/jie/Develop/hechun/app/docs/superpowers/specs/2026-06-02-ai-assistant-chat-design.md``
(§3.1, M1.5 (b)).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from kimi_cli.soul.http_skill_runtime import (
    DEFAULT_TIMEOUT_SECONDS,
    EnvSubstitutionError,
    HTTPSkillRuntime,
    ToolYamlError,
    discover_http_skill_specs,
    parse_tool_yaml,
    substitute_env,
)

# ---------------------------------------------------------------------------
# parse_tool_yaml
# ---------------------------------------------------------------------------


_BOLUS_YAML = """\
name: bolus_calc
description: Calculate suggested bolus dose.
parameters:
  type: object
  required: [carbs_g]
  properties:
    carbs_g: { type: number }
runtime:
  type: http
  url: ${HECHUN_INTERNAL_BASE_URL}/api/v1/internal/ai/tool/bolus_calc
  method: POST
  headers:
    Authorization: Bearer ${INTERNAL_API_TOKEN}
    X-User-Id: ${KIMI_USER_ID}
  body: $params
  timeout_seconds: 20
"""


def _write(p: Path, content: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_parse_tool_yaml_happy_path(tmp_path: Path) -> None:
    f = _write(tmp_path / "tool.yaml", _BOLUS_YAML)
    spec = parse_tool_yaml(f)
    assert spec.name == "bolus_calc"
    assert spec.parameters["required"] == ["carbs_g"]
    assert spec.runtime.url.startswith("${HECHUN_INTERNAL_BASE_URL}")
    assert spec.runtime.method == "POST"
    assert spec.runtime.timeout_seconds == 20.0
    assert spec.runtime.headers["Authorization"] == "Bearer ${INTERNAL_API_TOKEN}"


def test_parse_tool_yaml_defaults_to_default_timeout(tmp_path: Path) -> None:
    f = _write(
        tmp_path / "tool.yaml",
        """\
name: noop
runtime:
  type: http
  url: http://example.org
  body: $params
""",
    )
    spec = parse_tool_yaml(f)
    assert spec.runtime.timeout_seconds == DEFAULT_TIMEOUT_SECONDS


def test_parse_tool_yaml_accepts_timeout_ms(tmp_path: Path) -> None:
    f = _write(
        tmp_path / "tool.yaml",
        """\
name: noop
runtime:
  type: http
  url: http://example.org
  body: $params
  timeout_ms: 5000
""",
    )
    spec = parse_tool_yaml(f)
    assert spec.runtime.timeout_seconds == 5.0


def test_parse_tool_yaml_rejects_non_http_runtime(tmp_path: Path) -> None:
    f = _write(
        tmp_path / "tool.yaml",
        """\
name: noop
runtime:
  type: stdio
  url: http://example.org
  body: $params
""",
    )
    with pytest.raises(ToolYamlError):
        parse_tool_yaml(f)


def test_parse_tool_yaml_rejects_non_params_body(tmp_path: Path) -> None:
    """Templated bodies are explicitly out of scope for M1.5."""
    f = _write(
        tmp_path / "tool.yaml",
        """\
name: noop
runtime:
  type: http
  url: http://example.org
  body: {fixed: 1}
""",
    )
    with pytest.raises(ToolYamlError):
        parse_tool_yaml(f)


def test_parse_tool_yaml_requires_name(tmp_path: Path) -> None:
    f = _write(
        tmp_path / "tool.yaml",
        """\
runtime:
  type: http
  url: http://example.org
  body: $params
""",
    )
    with pytest.raises(ToolYamlError):
        parse_tool_yaml(f)


# ---------------------------------------------------------------------------
# substitute_env
# ---------------------------------------------------------------------------


def test_substitute_env_simple() -> None:
    out = substitute_env("X=${A}", environ={"A": "v"})
    assert out == "X=v"


def test_substitute_env_default_used_when_missing() -> None:
    out = substitute_env("X=${A:-fallback}", environ={})
    assert out == "X=fallback"


def test_substitute_env_default_skipped_when_present() -> None:
    out = substitute_env("X=${A:-fallback}", environ={"A": "real"})
    assert out == "X=real"


def test_substitute_env_missing_raises() -> None:
    with pytest.raises(EnvSubstitutionError) as exc_info:
        substitute_env("X=${ABSENT}", environ={})
    assert "ABSENT" in str(exc_info.value)


def test_substitute_env_multiple_missing_reported() -> None:
    """When several placeholders fail we report them all in one error so
    the dev fixes them in one go instead of one-error-per-restart."""
    with pytest.raises(EnvSubstitutionError) as exc_info:
        substitute_env("${A}/${B}", environ={})
    msg = str(exc_info.value)
    assert "A" in msg
    assert "B" in msg


# ---------------------------------------------------------------------------
# HTTPSkillRuntime end-to-end (mocked transport)
# ---------------------------------------------------------------------------


def _spec_from_yaml(tmp_path: Path, yaml_text: str = _BOLUS_YAML):
    return parse_tool_yaml(_write(tmp_path / "tool.yaml", yaml_text))


@pytest.fixture
def env_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the env vars referenced by the bolus_calc fixture yaml."""
    monkeypatch.setenv("HECHUN_INTERNAL_BASE_URL", "http://backend")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-token")
    monkeypatch.setenv("KIMI_USER_ID", "user-1")


def _mock_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Swap httpx.AsyncClient at the module import site to use a mock
    transport.  Captures the real class first so the replacement isn't
    self-recursive on subsequent invocations within the test."""
    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def _make(**kw: object) -> httpx.AsyncClient:
        kw.pop("transport", None)
        return real_cls(transport=transport, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr("kimi_cli.soul.http_skill_runtime.httpx.AsyncClient", _make)


@pytest.mark.anyio
async def test_http_skill_runtime_happy_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env_setup: None
) -> None:
    """LLM call → 200 JSON → ``ToolOk`` with serialized JSON body."""
    spec = _spec_from_yaml(tmp_path)
    tool = HTTPSkillRuntime(spec)

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = request.content
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={"suggested_units": 4.5, "rationale": "..."})

    _mock_transport(monkeypatch, handler)

    result = await tool(carbs_g=60)
    from kosong.tooling import ToolOk

    assert isinstance(result, ToolOk)
    assert "suggested_units" in str(result.output)
    assert captured["method"] == "POST"
    assert captured["url"] == "http://backend/api/v1/internal/ai/tool/bolus_calc"
    assert captured["headers"]["authorization"] == "Bearer test-token"
    assert captured["headers"]["x-user-id"] == "user-1"
    # Body should be the params dict as JSON.
    import json

    assert json.loads(captured["body"]) == {"carbs_g": 60}


@pytest.mark.anyio
async def test_http_skill_runtime_4xx_passes_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env_setup: None
) -> None:
    """4xx must reach the LLM as a ``ToolError`` (no retry)."""
    spec = _spec_from_yaml(tmp_path)
    tool = HTTPSkillRuntime(spec)

    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(400, text="bad input: carbs_g must be > 0")

    _mock_transport(monkeypatch, handler)

    result = await tool(carbs_g=-1)
    from kosong.tooling import ToolError

    assert isinstance(result, ToolError)
    assert "400" in result.message
    assert attempts["count"] == 1  # no retry on 4xx


@pytest.mark.anyio
async def test_http_skill_runtime_5xx_retries_then_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env_setup: None
) -> None:
    spec = _spec_from_yaml(tmp_path)
    tool = HTTPSkillRuntime(spec)

    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(503, text="overloaded")

    _mock_transport(monkeypatch, handler)

    result = await tool(carbs_g=60)
    from kosong.tooling import ToolError

    assert isinstance(result, ToolError)
    assert attempts["count"] == 2  # 1 try + 1 retry


@pytest.mark.anyio
async def test_http_skill_runtime_5xx_then_200_recovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env_setup: None
) -> None:
    """Transient 5xx followed by 200 → ToolOk (retry path covers it)."""
    spec = _spec_from_yaml(tmp_path)
    tool = HTTPSkillRuntime(spec)

    responses = iter(
        [
            httpx.Response(502, text="bad gateway"),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    _mock_transport(monkeypatch, handler)

    result = await tool(carbs_g=60)
    from kosong.tooling import ToolOk

    assert isinstance(result, ToolOk)


@pytest.mark.anyio
async def test_http_skill_runtime_env_missing_returns_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No env vars → ``ToolError`` instead of crashing the agent loop."""
    # Wipe out the env vars so substitution fails.
    monkeypatch.delenv("HECHUN_INTERNAL_BASE_URL", raising=False)
    monkeypatch.delenv("INTERNAL_API_TOKEN", raising=False)
    monkeypatch.delenv("KIMI_USER_ID", raising=False)

    spec = _spec_from_yaml(tmp_path)
    tool = HTTPSkillRuntime(spec)

    result = await tool(carbs_g=60)
    from kosong.tooling import ToolError

    assert isinstance(result, ToolError)
    assert "Missing env" in result.message


@pytest.mark.anyio
async def test_http_skill_runtime_timeout_retries_and_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env_setup: None
) -> None:
    """Consistent timeout → 1 retry → ``ToolError`` with transport tag."""
    spec = _spec_from_yaml(tmp_path)

    attempts = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        raise httpx.ConnectTimeout("simulated timeout")

    _mock_transport(monkeypatch, handler)

    tool = HTTPSkillRuntime(spec)
    result = await tool(carbs_g=60)
    from kosong.tooling import ToolError

    assert isinstance(result, ToolError)
    assert attempts["count"] == 2  # 1 try + 1 retry
    assert "transport failure" in result.message


# ---------------------------------------------------------------------------
# discover_http_skill_specs
# ---------------------------------------------------------------------------


def test_discover_http_skill_specs_finds_valid_yamls(tmp_path: Path) -> None:
    bolus_dir = tmp_path / "bolus_calc"
    bg_dir = tmp_path / "bg_interpret"
    _write(bolus_dir / "tool.yaml", _BOLUS_YAML)
    _write(bg_dir / "tool.yaml", _BOLUS_YAML.replace("name: bolus_calc", "name: bg_interpret"))
    # A skill without a tool.yaml is ignored (most existing kimo skills).
    (tmp_path / "no_tool").mkdir()
    (tmp_path / "no_tool" / "SKILL.md").write_text("# nothing", encoding="utf-8")

    specs = discover_http_skill_specs([bolus_dir, bg_dir, tmp_path / "no_tool"])
    names = sorted(s.name for s in specs)
    assert names == ["bg_interpret", "bolus_calc"]


def test_discover_http_skill_specs_skips_bad_yaml(tmp_path: Path) -> None:
    """A malformed yaml under one skill must not poison the rest."""
    bolus_dir = tmp_path / "bolus_calc"
    bad_dir = tmp_path / "bad"
    _write(bolus_dir / "tool.yaml", _BOLUS_YAML)
    _write(bad_dir / "tool.yaml", "this: is: not: a: valid: mapping: yaml: !@#%")

    specs = discover_http_skill_specs([bolus_dir, bad_dir])
    # bolus_calc still registered; the bad one is silently dropped.
    assert [s.name for s in specs] == ["bolus_calc"]


def test_discover_http_skill_specs_dedupes_by_name(tmp_path: Path) -> None:
    dir_a = tmp_path / "a" / "bolus_calc"
    dir_b = tmp_path / "b" / "bolus_calc"
    _write(dir_a / "tool.yaml", _BOLUS_YAML)
    _write(dir_b / "tool.yaml", _BOLUS_YAML)

    specs = discover_http_skill_specs([dir_a, dir_b])
    assert [s.name for s in specs] == ["bolus_calc"]
