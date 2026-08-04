from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast, get_args

import yaml
from kosong.chat_provider import ChatProvider
from pydantic import SecretStr

from kimi_cli.constant import USER_AGENT
from kimi_cli.utils.logging import logger

if TYPE_CHECKING:
    from kimi_cli.auth.oauth import OAuthManager
    from kimi_cli.config import Config, LLMModel, LLMProvider

type ProviderType = Literal[
    "kimi",
    "openai_legacy",
    "openai_responses",
    "anthropic",
    "google_genai",  # for backward-compatibility, equals to `gemini`
    "gemini",
    "vertexai",
    "_echo",
    "_scripted_echo",
    "_chaos",
]

type ModelCapability = Literal["image_in", "video_in", "thinking", "always_thinking"]
ALL_MODEL_CAPABILITIES: set[ModelCapability] = set(get_args(ModelCapability.__value__))


@dataclass(slots=True)
class LLM:
    chat_provider: ChatProvider
    max_context_size: int
    capabilities: set[ModelCapability]
    model_config: LLMModel | None = None
    provider_config: LLMProvider | None = None

    @property
    def model_name(self) -> str:
        return self.chat_provider.model_name


_HECHUN_OWNER_PREFIX = "hechun-"


def _feature_from_subagent() -> str:
    """Map the ``SUBAGENT`` env var to an accounting ``feature`` (K2).

    hechun/avocado forwards ``SUBAGENT`` into the sandbox container. The digest
    (血糖解读) agent name contains "digest" (e.g. ``diabetes-digest-slim``); any
    other agent (default agent with no SUBAGENT, or ``diabetes-expert``) is
    treated as chat.
    """
    sub = (os.getenv("SUBAGENT") or "").strip().lower()
    if "digest" in sub:
        return "digest"
    return "chat"


def build_request_metadata(turn_id: str | None) -> dict[str, str] | None:
    """Build the litellm request ``metadata`` for token accounting (K1/K2/K3).

    Reads ``KIMI_USER_ID`` (set by the sandbox runner from the session owner_id,
    shaped like ``hechun-<bigint>``) and strips the ``hechun-`` prefix so the §6
    collector's ``parseLong(metadata.user_id)`` succeeds (坑①). If no usable
    user_id can be derived (anonymous / ``webui-<uuid>`` / empty), returns
    ``None`` so no metadata is injected.

    Args:
        turn_id: canonical per-turn id; included only when present.
    """
    raw = (os.getenv("KIMI_USER_ID") or "").strip()
    user_id = raw[len(_HECHUN_OWNER_PREFIX) :] if raw.startswith(_HECHUN_OWNER_PREFIX) else raw
    if not user_id:
        return None
    md: dict[str, str] = {"user_id": user_id, "feature": _feature_from_subagent()}
    if turn_id:
        md["turn_id"] = turn_id
    return md


def apply_request_metadata(chat_provider: ChatProvider, turn_id: str | None) -> ChatProvider:
    """Derive a per-request provider copy carrying token-accounting metadata (K3).

    Uses kosong's immutable ``with_extra_body`` builder so the session-baseline
    provider is never mutated, and the top-level ``metadata`` key is replaced
    without clobbering sibling keys (e.g. ``thinking``). Providers without
    ``with_extra_body`` (e.g. anthropic, which injects user_id at construction)
    are returned unchanged.
    """
    md = build_request_metadata(turn_id)
    if md is None:
        return chat_provider
    with_extra_body = getattr(chat_provider, "with_extra_body", None)
    if with_extra_body is not None:
        return cast(ChatProvider, with_extra_body({"metadata": md}))
    return chat_provider


def parse_llm_providers_env() -> list[dict[str, Any]] | None:
    """Parse ``LLM_PROVIDERS`` environment variable as a YAML list.

    Expected format (YAML string inside an env var)::

        LLM_PROVIDERS='
        - name: kimi
          type: kimi
          api_key: sk-...
          base_url: https://api.moonshot.cn/v1
          model: kimi-k2
          max_context_size: 262144
          capabilities:
            - thinking
            - image_in
        '

    Returns:
        A list of provider dictionaries, or ``None`` if the variable is not
        set or cannot be parsed.
    """
    raw = os.environ.get("LLM_PROVIDERS")
    if not raw:
        return None
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        logger.warning("Failed to parse LLM_PROVIDERS as YAML: {exc}", exc=exc)
        return None
    if not isinstance(data, list):
        logger.warning("LLM_PROVIDERS must be a YAML list, got {type}", type=type(data).__name__)
        return None
    return cast(list[dict[str, Any]], data)


def model_display_name(model_name: str | None, model: LLMModel | None = None) -> str:
    if model is not None and model.display_name:
        return model.display_name
    if not model_name:
        return ""
    if model_name in ("kimi-for-coding", "kimi-code"):
        return "kimi-for-coding"
    return model_name


def augment_provider_with_env_vars(provider: LLMProvider, model: LLMModel) -> dict[str, str]:
    """Override provider/model settings from environment variables.

    Returns:
        Mapping of environment variables that were applied.
    """
    applied: dict[str, str] = {}

    match provider.type:
        case "kimi":
            if base_url := os.getenv("KIMI_BASE_URL"):
                provider.base_url = base_url
                applied["KIMI_BASE_URL"] = base_url
            if api_key := os.getenv("KIMI_API_KEY"):
                provider.api_key = SecretStr(api_key)
                applied["KIMI_API_KEY"] = "******"
            if model_name := os.getenv("KIMI_MODEL_NAME"):
                model.model = model_name
                applied["KIMI_MODEL_NAME"] = model_name
            if max_context_size := os.getenv("KIMI_MODEL_MAX_CONTEXT_SIZE"):
                model.max_context_size = int(max_context_size)
                applied["KIMI_MODEL_MAX_CONTEXT_SIZE"] = max_context_size
            if capabilities := os.getenv("KIMI_MODEL_CAPABILITIES"):
                caps_lower = (cap.strip().lower() for cap in capabilities.split(",") if cap.strip())
                model.capabilities = set(
                    cast(ModelCapability, cap)
                    for cap in caps_lower
                    if cap in get_args(ModelCapability.__value__)
                )
                applied["KIMI_MODEL_CAPABILITIES"] = capabilities
        case "openai_legacy" | "openai_responses":
            # Only fill missing values from env vars; do not override
            # explicit config.toml settings (e.g. local providers)
            if not provider.base_url and (base_url := os.getenv("OPENAI_BASE_URL")):
                provider.base_url = base_url
            if not provider.api_key.get_secret_value() and (api_key := os.getenv("OPENAI_API_KEY")):
                provider.api_key = SecretStr(api_key)
            if model_name := os.getenv("OPENAI_MODEL_NAME"):
                model.model = model_name
                applied["OPENAI_MODEL_NAME"] = model_name
        case "anthropic":
            if not provider.base_url and (base_url := os.getenv("ANTHROPIC_BASE_URL")):
                provider.base_url = base_url
            if not provider.api_key.get_secret_value() and (
                api_key := os.getenv("ANTHROPIC_API_KEY")
            ):
                provider.api_key = SecretStr(api_key)
            if model_name := os.getenv("ANTHROPIC_MODEL_NAME"):
                model.model = model_name
                applied["ANTHROPIC_MODEL_NAME"] = model_name
        case _:
            pass

    # Model capabilities can be overridden for any provider
    if capabilities := os.getenv("KIMI_MODEL_CAPABILITIES"):
        caps_lower = (cap.strip().lower() for cap in capabilities.split(",") if cap.strip())
        model.capabilities = set(
            cast(ModelCapability, cap)
            for cap in caps_lower
            if cap in get_args(ModelCapability.__value__)
        )
        applied["KIMI_MODEL_CAPABILITIES"] = capabilities

    return applied


def _kimi_default_headers(provider: LLMProvider, oauth: OAuthManager | None) -> dict[str, str]:
    headers = {"User-Agent": USER_AGENT}
    if oauth:
        headers.update(oauth.common_headers())
    if provider.custom_headers:
        headers.update(provider.custom_headers)
    return headers


def create_llm(
    provider: LLMProvider,
    model: LLMModel,
    *,
    thinking: bool | None = None,
    session_id: str | None = None,
    oauth: OAuthManager | None = None,
) -> LLM | None:
    if provider.type not in {"_echo", "_scripted_echo"} and (
        not provider.base_url or not model.model
    ):
        logger.warning(
            "Cannot create LLM: missing base_url or model (provider_type={provider_type})",
            provider_type=provider.type,
        )
        return None

    resolved_api_key = (
        oauth.resolve_api_key(provider.api_key, provider.oauth)
        if oauth and provider.oauth
        else provider.api_key.get_secret_value()
    )

    match provider.type:
        case "kimi":
            from kosong.chat_provider.kimi import Kimi

            chat_provider = Kimi(
                model=model.model,
                base_url=provider.base_url,
                api_key=resolved_api_key,
                default_headers=_kimi_default_headers(provider, oauth),
            )

            gen_kwargs: Kimi.GenerationKwargs = {}
            if session_id:
                gen_kwargs["prompt_cache_key"] = session_id
            if temperature := os.getenv("KIMI_MODEL_TEMPERATURE"):
                gen_kwargs["temperature"] = float(temperature)
            if top_p := os.getenv("KIMI_MODEL_TOP_P"):
                gen_kwargs["top_p"] = float(top_p)
            if max_tokens := os.getenv("KIMI_MODEL_MAX_TOKENS"):
                gen_kwargs["max_tokens"] = int(max_tokens)

            if gen_kwargs:
                chat_provider = chat_provider.with_generation_kwargs(**gen_kwargs)

            # K1/K2: baseline token-accounting metadata (user_id + feature, no
            # turn_id). Guarantees litellm gets user_id+feature even when the
            # wire turn_id is not delivered (auto-start / old backend); the
            # per-turn turn_id is layered on in KimiSoul._step via
            # apply_request_metadata (which replaces the whole `metadata` key).
            base_md = build_request_metadata(None)
            if base_md:
                chat_provider = chat_provider.with_extra_body({"metadata": base_md})
        case "openai_legacy":
            from kosong.contrib.chat_provider.openai_legacy import OpenAILegacy

            reasoning_key = (
                provider.reasoning_key
                if provider.reasoning_key is not None
                else "reasoning_content"
            )
            chat_provider = OpenAILegacy(
                model=model.model,
                base_url=provider.base_url,
                api_key=resolved_api_key,
                reasoning_key=reasoning_key,
                default_headers=dict(provider.custom_headers) if provider.custom_headers else None,
            )
        case "openai_responses":
            from kosong.contrib.chat_provider.openai_responses import OpenAIResponses

            chat_provider = OpenAIResponses(
                model=model.model,
                base_url=provider.base_url,
                api_key=resolved_api_key,
                default_headers=dict(provider.custom_headers) if provider.custom_headers else None,
            )
        case "anthropic":
            from kosong.contrib.chat_provider.anthropic import Anthropic

            chat_provider = Anthropic(
                model=model.model,
                base_url=provider.base_url,
                api_key=resolved_api_key,
                default_max_tokens=50000,
                metadata={"user_id": session_id} if session_id else None,
                default_headers=dict(provider.custom_headers) if provider.custom_headers else None,
            )
        case "google_genai" | "gemini":
            from kosong.contrib.chat_provider.google_genai import GoogleGenAI

            chat_provider = GoogleGenAI(
                model=model.model,
                base_url=provider.base_url,
                api_key=resolved_api_key,
                default_headers=dict(provider.custom_headers) if provider.custom_headers else None,
            )
        case "vertexai":
            from kosong.contrib.chat_provider.google_genai import GoogleGenAI

            os.environ.update(provider.env or {})
            chat_provider = GoogleGenAI(
                model=model.model,
                base_url=provider.base_url,
                api_key=resolved_api_key,
                vertexai=True,
                default_headers=dict(provider.custom_headers) if provider.custom_headers else None,
            )
        case "_echo":
            from kosong.chat_provider.echo import EchoChatProvider

            chat_provider = EchoChatProvider()
        case "_scripted_echo":
            from kosong.chat_provider.echo import ScriptedEchoChatProvider

            if provider.env:
                os.environ.update(provider.env)
            scripts = _load_scripted_echo_scripts()
            trace_value = os.getenv("KIMI_SCRIPTED_ECHO_TRACE", "")
            trace = trace_value.strip().lower() in {"1", "true", "yes", "on"}
            chat_provider = ScriptedEchoChatProvider(scripts, trace=trace)
        case "_chaos":
            from kosong.chat_provider.chaos import ChaosChatProvider, ChaosConfig
            from kosong.chat_provider.kimi import Kimi

            chat_provider = ChaosChatProvider(
                provider=Kimi(
                    model=model.model,
                    base_url=provider.base_url,
                    api_key=resolved_api_key,
                    default_headers=_kimi_default_headers(provider, oauth),
                ),
                chaos_config=ChaosConfig(
                    error_probability=0.8,
                    error_types=[429, 500, 503],
                ),
            )

    capabilities = derive_model_capabilities(model)

    # Apply thinking if specified or if model always requires thinking
    thinking_on = "always_thinking" in capabilities or (
        thinking is True and "thinking" in capabilities
    )
    if thinking_on:
        chat_provider = chat_provider.with_thinking("high")
    elif thinking is False:
        chat_provider = chat_provider.with_thinking("off")
    # If thinking is None and model doesn't always think, leave as-is (default behavior)

    # Apply Moonshot-specific ``thinking.keep`` (preserved thinking) only when
    # the model is actually in thinking mode; otherwise the API would see a
    # ``thinking.keep`` without an accompanying ``thinking.type`` it honors.
    if thinking_on and provider.type == "kimi":
        from kosong.chat_provider.kimi import Kimi

        if isinstance(chat_provider, Kimi) and (
            thinking_keep := os.getenv("KIMI_MODEL_THINKING_KEEP")
        ):
            chat_provider = chat_provider.with_extra_body({"thinking": {"keep": thinking_keep}})

    return LLM(
        chat_provider=chat_provider,
        max_context_size=model.max_context_size,
        capabilities=capabilities,
        model_config=model,
        provider_config=provider,
    )


def clone_llm_with_model_alias(
    llm: LLM | None,
    config: Config,
    model_alias: str | None,
    *,
    session_id: str,
    oauth: OAuthManager | None,
) -> LLM | None:
    if model_alias is None:
        return llm
    if model_alias not in config.models:
        raise KeyError(f"Unknown model alias: {model_alias}")
    model = config.models[model_alias]
    provider = config.providers[model.provider]
    thinking: bool | None = None
    if llm is not None:
        effort = getattr(llm.chat_provider, "thinking_effort", None)
        if effort is not None:
            thinking = effort != "off"
    return create_llm(
        provider,
        model,
        thinking=thinking,
        session_id=session_id,
        oauth=oauth,
    )


def derive_model_capabilities(model: LLMModel) -> set[ModelCapability]:
    capabilities = set(model.capabilities or ())
    # Models with "thinking" in their name are always-thinking models
    if "thinking" in model.model.lower() or "reason" in model.model.lower():
        capabilities.update(("thinking", "always_thinking"))
    # These models support thinking but can be toggled on/off
    elif model.model in {"kimi-for-coding", "kimi-code"}:
        capabilities.update(("thinking", "image_in", "video_in"))
    # kimi-k2 supports thinking, image_in, and video_in
    if model.model.lower().startswith("kimi-k2"):
        capabilities.update(("thinking", "image_in", "video_in"))
    # OpenAI GPT-4o and o1/o3 series support vision
    if any(name in model.model.lower() for name in ("gpt-4o", "o1", "o3")):
        capabilities.add("image_in")
    # Anthropic Claude 3/3.5/3.7 supports vision
    if "claude-3" in model.model.lower():
        capabilities.add("image_in")
    return capabilities


def _load_scripted_echo_scripts() -> list[str]:
    script_path = os.getenv("KIMI_SCRIPTED_ECHO_SCRIPTS")
    if not script_path:
        raise ValueError("KIMI_SCRIPTED_ECHO_SCRIPTS is required for _scripted_echo.")
    path = Path(script_path).expanduser()
    if not path.exists():
        raise ValueError(f"Scripted echo file not found: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        data: object = json.loads(text)
    except json.JSONDecodeError:
        scripts = [chunk.strip() for chunk in text.split("\n---\n") if chunk.strip()]
        if scripts:
            return scripts
        raise ValueError(
            "Scripted echo file must be a JSON array of strings or a text file "
            "split by '\\n---\\n'."
        ) from None
    if isinstance(data, list):
        data_list = cast(list[object], data)
        if all(isinstance(item, str) for item in data_list):
            return cast(list[str], data_list)
    raise ValueError("Scripted echo JSON must be an array of strings.")
