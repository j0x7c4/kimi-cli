"""HTTP-runtime skill tools.

A skill (canonical SKILL.md layout) optionally ships a ``tool.yaml`` next
to its ``SKILL.md``.  If that ``tool.yaml`` declares ``runtime.type:
http``, kimi-cli registers an :class:`HTTPSkillRuntime` callable so the
LLM can invoke it as a regular tool — the call materialises as an
outbound HTTPS request to whatever endpoint the yaml points at.

This is what makes the hechun (avocado) integration's 5 wrapped backend
calls — ``emit_tool_card`` / ``bolus_calc`` / ``bg_interpret`` /
``hba1c_predict`` / ``exercise_suggest`` — actually callable instead of
"just discoverable".  See ``custom-skills/hechun/<skill>/tool.yaml``.

Spec ref: ``2026-06-02-ai-assistant-chat-design.md`` (§3.1) under
``/Users/jie/Develop/hechun/app/docs/superpowers/specs/``.

Design notes
------------

* **Env var substitution** — values inside ``${...}`` placeholders in
  ``url`` / ``headers`` are resolved from ``os.environ`` at call time
  (not registration time), so a single registered tool can serve
  multiple user sessions whose ``KIMI_USER_ID`` / ``HECHUN_*`` differ.
  Missing env vars surface as a tool-side error rather than crashing
  the agent — the LLM sees a clear "$VAR is unset" message and can
  apologise instead of silently calling the wrong endpoint.

* **Body shape** — the only supported ``body`` value today is the
  literal string ``$params``, which serialises the LLM's argument dict
  as a single JSON body.  This matches the spec's tool.yaml examples
  and the hechun ``/internal/ai/tool/{name}`` contract; arbitrary
  templates are deliberately deferred.

* **Retry policy** — one retry for 5xx / connect errors / timeouts; 4xx
  goes straight back to the LLM as a ``ToolError`` so it can adjust its
  arguments.  Anything else (e.g. JSON decode failure on response) is
  reported as ``ToolError`` with the raw body truncated for context.

* **Conflict policy** — registration refuses to clobber a built-in tool
  or a previously-registered HTTPSkillRuntime with the same name.  The
  caller logs and continues.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml
from kosong.tooling import CallableTool, ToolError, ToolOk, ToolReturnValue

from kimi_cli import logger

# Default timeout when ``tool.yaml`` doesn't specify one.  30s matches
# the spec's stated default for backend calls; long enough for bolus
# calc / HbA1c predict (which may touch the DB + an LLM provider), short
# enough that a hung backend doesn't permanently wedge the agent.
DEFAULT_TIMEOUT_SECONDS = 30.0

# Cap raw body inclusion in error messages so we don't blow up the LLM
# context with megabyte HTML error pages.
_MAX_ERROR_BODY_CHARS = 2048

# Match ``${NAME}`` or ``${NAME:-default}``.  The default form is a
# convenience for non-required env vars (``${KIMI_USER_ID:-anonymous}``).
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ToolYamlError(ValueError):
    """``tool.yaml`` is malformed or missing required fields."""


class EnvSubstitutionError(RuntimeError):
    """A required ``${VAR}`` placeholder couldn't be resolved from env."""


@dataclass(frozen=True, slots=True)
class HTTPRuntimeSpec:
    """Parsed ``runtime:`` block from ``tool.yaml`` — only the bits we use."""

    url: str
    method: str
    headers: dict[str, str]
    timeout_seconds: float
    body_mode: str
    """Currently always ``"params"`` (the literal ``$params`` body)."""


@dataclass(frozen=True, slots=True)
class ToolYamlSpec:
    """Fully-parsed ``tool.yaml`` ready to hand to ``HTTPSkillRuntime``."""

    name: str
    description: str
    parameters: dict[str, Any]
    runtime: HTTPRuntimeSpec


def parse_tool_yaml(path: Path) -> ToolYamlSpec:
    """Parse a ``tool.yaml`` file describing an HTTP-runtime skill.

    Raises:
        ToolYamlError: When the file is unreadable, isn't valid YAML, or
            is missing required fields (``name``, ``runtime.url``).  The
            caller logs and continues so a single bad yaml doesn't
            poison the rest of the skill discovery pass.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolYamlError(f"Cannot read {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ToolYamlError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ToolYamlError(f"{path}: top-level must be a mapping")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ToolYamlError(f"{path}: ``name`` is required")

    description = data.get("description") or ""
    if not isinstance(description, str):
        raise ToolYamlError(f"{path}: ``description`` must be a string")

    parameters = data.get("parameters") or {"type": "object", "properties": {}}
    if not isinstance(parameters, dict):
        raise ToolYamlError(f"{path}: ``parameters`` must be a mapping")

    runtime_raw = data.get("runtime")
    if not isinstance(runtime_raw, dict):
        raise ToolYamlError(f"{path}: ``runtime`` block is required")

    rtype = runtime_raw.get("type")
    if rtype != "http":
        raise ToolYamlError(f"{path}: only ``runtime.type: http`` is supported (got {rtype!r})")

    url = runtime_raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ToolYamlError(f"{path}: ``runtime.url`` is required")

    method = str(runtime_raw.get("method", "POST")).upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ToolYamlError(f"{path}: unsupported method {method!r}")

    headers_raw = runtime_raw.get("headers") or {}
    if not isinstance(headers_raw, dict):
        raise ToolYamlError(f"{path}: ``runtime.headers`` must be a mapping")
    headers = {str(k): str(v) for k, v in headers_raw.items()}

    # Both ``timeout_seconds`` (the form the hechun bundle uses) and
    # ``timeout_ms`` are accepted so we don't lock the spec into one.
    timeout_seconds = float(DEFAULT_TIMEOUT_SECONDS)
    if "timeout_seconds" in runtime_raw:
        timeout_seconds = float(runtime_raw["timeout_seconds"])
    elif "timeout_ms" in runtime_raw:
        timeout_seconds = float(runtime_raw["timeout_ms"]) / 1000.0

    body_field = runtime_raw.get("body", "$params")
    if body_field != "$params":
        raise ToolYamlError(
            f"{path}: only ``body: $params`` is supported in M1.5; got {body_field!r}"
        )

    return ToolYamlSpec(
        name=name.strip(),
        description=description.strip() or "No description provided.",
        parameters=parameters,
        runtime=HTTPRuntimeSpec(
            url=url,
            method=method,
            headers=headers,
            timeout_seconds=timeout_seconds,
            body_mode="params",
        ),
    )


def substitute_env(template: str, *, environ: dict[str, str] | None = None) -> str:
    """Expand ``${NAME}`` (and ``${NAME:-default}``) against env.

    Raises:
        EnvSubstitutionError: When a placeholder without a default
            references an env var that isn't set.  We fail loudly rather
            than silently substituting ``""`` because an empty URL or
            Authorization header is almost always wrong (the request
            would hit a different host or 401 without explanation).
    """
    env = environ if environ is not None else os.environ
    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default = match.group(2)
        value = env.get(var_name)
        if value is not None:
            return value
        if default is not None:
            return default
        missing.append(var_name)
        return ""

    result = _ENV_VAR_PATTERN.sub(_replace, template)
    if missing:
        raise EnvSubstitutionError(
            f"Missing env var(s) for HTTP skill runtime: {', '.join(sorted(set(missing)))}"
        )
    return result


class HTTPSkillRuntime(CallableTool):
    """A skill whose ``tool.yaml`` declares ``runtime.type: http``.

    The LLM sees this as a normal tool; on call we POST (or whatever the
    yaml asks for) to the resolved URL with the LLM's argument dict as
    the JSON body.  The HTTP status / body is translated to ``ToolOk`` /
    ``ToolError`` so the LLM can reason about success vs failure.
    """

    def __init__(self, spec: ToolYamlSpec) -> None:
        super().__init__(
            name=spec.name,
            description=spec.description,
            parameters=spec.parameters,
        )
        # Pydantic BaseModel: extra fields need to bypass schema; using
        # ``object.__setattr__`` works around the model's __setattr__.
        object.__setattr__(self, "_spec", spec)

    @property
    def spec(self) -> ToolYamlSpec:
        return self._spec  # type: ignore[attr-defined]

    async def __call__(self, *args: Any, **kwargs: Any) -> ToolReturnValue:
        spec: ToolYamlSpec = self._spec  # type: ignore[attr-defined]
        # The LLM gives us either positional or keyword args depending on
        # the model — for HTTP skills we always want the kwargs dict to
        # become the JSON body.  Positional-only is not meaningful here;
        # we error out so the model corrects the call shape.
        if args and not kwargs:
            return ToolError(
                message=(
                    f"HTTP skill `{spec.name}` requires JSON-object arguments, not positional."
                ),
                brief="Invalid call shape",
            )
        params = dict(kwargs)

        try:
            url = substitute_env(spec.runtime.url)
            headers = {k: substitute_env(v) for k, v in spec.runtime.headers.items()}
        except EnvSubstitutionError as exc:
            logger.warning(
                "HTTP skill `{name}` env substitution failed: {error}",
                name=spec.name,
                error=exc,
            )
            return ToolError(message=str(exc), brief="env unresolved")

        # Always announce JSON Content-Type unless caller overrode it.
        headers.setdefault("Content-Type", "application/json")

        return await _do_http_call(spec, url, headers, params)


async def _do_http_call(
    spec: ToolYamlSpec,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any],
) -> ToolReturnValue:
    """Run the actual HTTP request with one retry on transient failures.

    Split out so tests can patch it without touching the
    ``CallableTool`` machinery.
    """
    timeout = httpx.Timeout(spec.runtime.timeout_seconds)
    last_error: str | None = None
    transient_attempts = 2  # 1 try + 1 retry

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(transient_attempts):
            try:
                response = await client.request(
                    spec.runtime.method,
                    url,
                    headers=headers,
                    json=params,
                )
            except (httpx.TimeoutException, httpx.TransportError, httpx.ConnectError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "HTTP skill `{name}` transport error (attempt {attempt}/{max}): {error}",
                    name=spec.name,
                    attempt=attempt + 1,
                    max=transient_attempts,
                    error=last_error,
                )
                if attempt + 1 == transient_attempts:
                    return ToolError(
                        message=f"HTTP skill `{spec.name}` transport failure: {last_error}",
                        brief="transport error",
                    )
                continue

            # --- 4xx: pass straight back to the LLM ---
            if 400 <= response.status_code < 500:
                body_excerpt = _truncate_body(response.text)
                return ToolError(
                    message=(f"HTTP {response.status_code} from `{spec.name}`: {body_excerpt}"),
                    brief=f"HTTP {response.status_code}",
                )

            # --- 5xx: retry once, then give up ---
            if 500 <= response.status_code < 600:
                body_excerpt = _truncate_body(response.text)
                last_error = f"HTTP {response.status_code}: {body_excerpt}"
                logger.warning(
                    "HTTP skill `{name}` server error (attempt {attempt}/{max}): {error}",
                    name=spec.name,
                    attempt=attempt + 1,
                    max=transient_attempts,
                    error=last_error,
                )
                if attempt + 1 == transient_attempts:
                    return ToolError(
                        message=f"HTTP skill `{spec.name}` failed after retry: {last_error}",
                        brief=f"HTTP {response.status_code}",
                    )
                continue

            # --- 2xx / 3xx ---
            return _response_to_tool_ok(spec, response)

    # Defensive fallback (loop always returns); satisfies the type checker.
    return ToolError(
        message=f"HTTP skill `{spec.name}` failed: {last_error or 'unknown error'}",
        brief="unknown error",
    )


def _response_to_tool_ok(spec: ToolYamlSpec, response: httpx.Response) -> ToolReturnValue:
    """Convert a 2xx response to ``ToolOk`` with body content.

    Backend contract says responses are JSON; we still tolerate plain
    text bodies (e.g. a deployment serving a 200 with empty body) by
    returning the raw text — the LLM is better at handling "weird but
    intact" data than "tool blew up".
    """
    body_text = response.text
    if response.headers.get("Content-Type", "").startswith("application/json"):
        try:
            parsed = response.json()
        except ValueError:
            logger.warning(
                "HTTP skill `{name}` returned non-JSON despite Content-Type",
                name=spec.name,
            )
            return ToolOk(output=_truncate_body(body_text))
        # Serialize with sorted keys + ensure_ascii=False so Chinese
        # tool_card payloads (the common hechun shape) survive intact.
        return ToolOk(output=json.dumps(parsed, ensure_ascii=False))
    return ToolOk(output=_truncate_body(body_text))


def _truncate_body(text: str) -> str:
    if len(text) <= _MAX_ERROR_BODY_CHARS:
        return text
    return text[:_MAX_ERROR_BODY_CHARS] + f"\n... [truncated; total {len(text)} chars]"


def discover_http_skill_specs(skill_dirs: list[Path]) -> list[ToolYamlSpec]:
    """Walk skill directories looking for ``<skill>/tool.yaml``.

    Returns the parsed specs ready to register.  Mirrors the layout used
    by ``custom-skills/hechun/<skill>/tool.yaml`` siblings of SKILL.md.
    Bad yaml files are logged and skipped — one borked file shouldn't
    take the whole agent down.
    """
    specs: list[ToolYamlSpec] = []
    seen_names: set[str] = set()
    for skill_dir in skill_dirs:
        if not skill_dir.is_dir():
            continue
        tool_yaml = skill_dir / "tool.yaml"
        if not tool_yaml.is_file():
            continue
        try:
            spec = parse_tool_yaml(tool_yaml)
        except ToolYamlError as exc:
            logger.warning(
                "Skipping HTTP skill at {path}: {error}",
                path=tool_yaml,
                error=exc,
            )
            continue
        if spec.name in seen_names:
            logger.warning(
                "Duplicate HTTP skill name `{name}` (at {path}); keeping first.",
                name=spec.name,
                path=tool_yaml,
            )
            continue
        seen_names.add(spec.name)
        specs.append(spec)
    return specs
