"""Unit tests for K-series token-accounting metadata helpers (K1/K2/K3).

Covers:
- build_request_metadata: hechun- prefix stripping (坑①), empty -> None,
  non-numeric owner passthrough, turn_id inclusion.
- _feature_from_subagent: digest / chat mapping (K2).
- apply_request_metadata: immutable per-turn derivation that does not mutate
  the baseline provider and does not clobber the thinking key.
- create_llm kimi branch: baseline metadata injection (K1).
- JSONRPCPromptMessage.Params.turn_id backward compatibility (K3-a).
"""

from __future__ import annotations

from pydantic import SecretStr

from kimi_cli.config import LLMModel, LLMProvider
from kimi_cli.llm import (
    _feature_from_subagent,
    apply_request_metadata,
    build_request_metadata,
    create_llm,
)


# --------------------------------------------------------------------------- #
# build_request_metadata (K1 user_id / 坑① prefix / turn_id)
# --------------------------------------------------------------------------- #
def test_build_request_metadata_strips_hechun_prefix(monkeypatch):
    monkeypatch.setenv("KIMI_USER_ID", "hechun-123456")
    monkeypatch.delenv("SUBAGENT", raising=False)
    md = build_request_metadata(None)
    assert md == {"user_id": "123456", "feature": "chat"}


def test_build_request_metadata_empty_user_returns_none(monkeypatch):
    monkeypatch.delenv("KIMI_USER_ID", raising=False)
    assert build_request_metadata(None) is None

    monkeypatch.setenv("KIMI_USER_ID", "")
    assert build_request_metadata(None) is None

    monkeypatch.setenv("KIMI_USER_ID", "   ")
    assert build_request_metadata(None) is None


def test_build_request_metadata_non_hechun_owner_passthrough(monkeypatch):
    # webui-<uuid> / plain owners: prefix not stripped, emitted as-is (§6
    # parseLong will fail and drop, which is acceptable — not a hechun user).
    monkeypatch.setenv("KIMI_USER_ID", "webui-abc")
    monkeypatch.delenv("SUBAGENT", raising=False)
    md = build_request_metadata(None)
    assert md == {"user_id": "webui-abc", "feature": "chat"}


def test_build_request_metadata_includes_turn_id_only_when_present(monkeypatch):
    monkeypatch.setenv("KIMI_USER_ID", "hechun-7")
    monkeypatch.delenv("SUBAGENT", raising=False)
    assert build_request_metadata(None) == {"user_id": "7", "feature": "chat"}
    assert build_request_metadata("") == {"user_id": "7", "feature": "chat"}
    assert build_request_metadata("t_abc") == {
        "user_id": "7",
        "feature": "chat",
        "turn_id": "t_abc",
    }


# --------------------------------------------------------------------------- #
# _feature_from_subagent (K2)
# --------------------------------------------------------------------------- #
def test_feature_from_subagent_digest(monkeypatch):
    monkeypatch.setenv("SUBAGENT", "diabetes-digest-slim")
    assert _feature_from_subagent() == "digest"


def test_feature_from_subagent_defaults_to_chat(monkeypatch):
    monkeypatch.delenv("SUBAGENT", raising=False)
    assert _feature_from_subagent() == "chat"

    monkeypatch.setenv("SUBAGENT", "diabetes-expert")
    assert _feature_from_subagent() == "chat"

    monkeypatch.setenv("SUBAGENT", "")
    assert _feature_from_subagent() == "chat"


def test_feature_from_subagent_case_insensitive(monkeypatch):
    monkeypatch.setenv("SUBAGENT", "Diabetes-DIGEST")
    assert _feature_from_subagent() == "digest"


# --------------------------------------------------------------------------- #
# apply_request_metadata (K3 per-turn derivation)
# --------------------------------------------------------------------------- #
def _make_thinking_kimi():
    from kimi_cli.llm import create_llm as _create

    provider = LLMProvider(
        type="kimi",
        base_url="https://api.test/v1",
        api_key=SecretStr("test-key"),
    )
    model = LLMModel(
        provider="kimi",
        model="kimi-k2-thinking-turbo",  # always-thinking -> extra_body.thinking set
        max_context_size=4096,
        capabilities=None,
    )
    llm = _create(provider, model)
    assert llm is not None
    return llm.chat_provider


def test_apply_request_metadata_derives_copy_without_mutating_original(monkeypatch):
    monkeypatch.delenv("KIMI_USER_ID", raising=False)  # baseline unset for a clean provider
    monkeypatch.delenv("SUBAGENT", raising=False)
    base = _make_thinking_kimi()
    base_extra = dict(base.model_parameters.get("extra_body") or {})
    assert "metadata" not in base_extra  # KIMI_USER_ID unset -> no baseline metadata
    assert base_extra.get("thinking", {}).get("type") == "enabled"

    # Now derive a per-turn copy with metadata present.
    monkeypatch.setenv("KIMI_USER_ID", "hechun-42")
    derived = apply_request_metadata(base, "t_turn1")

    # Derived carries metadata + preserves the thinking key.
    derived_extra = derived.model_parameters.get("extra_body") or {}
    assert derived_extra.get("metadata") == {
        "user_id": "42",
        "feature": "chat",
        "turn_id": "t_turn1",
    }
    assert derived_extra.get("thinking", {}).get("type") == "enabled"

    # Original provider is untouched (immutable builder).
    assert base is not derived
    assert (base.model_parameters.get("extra_body") or {}) == base_extra
    assert "metadata" not in (base.model_parameters.get("extra_body") or {})


def test_apply_request_metadata_no_user_id_returns_same_provider(monkeypatch):
    monkeypatch.delenv("KIMI_USER_ID", raising=False)
    base = _make_thinking_kimi()
    result = apply_request_metadata(base, "t_turn1")
    assert result is base  # None metadata -> unchanged (identity)


def test_apply_request_metadata_provider_without_with_extra_body(monkeypatch):
    monkeypatch.setenv("KIMI_USER_ID", "hechun-9")
    monkeypatch.delenv("SUBAGENT", raising=False)

    class _NoExtraBody:
        """Stand-in for a provider (e.g. anthropic) lacking with_extra_body."""

    p = _NoExtraBody()
    assert apply_request_metadata(p, "t_x") is p


# --------------------------------------------------------------------------- #
# create_llm kimi baseline injection (K1)
# --------------------------------------------------------------------------- #
def test_create_llm_kimi_injects_baseline_metadata(monkeypatch):
    monkeypatch.setenv("KIMI_USER_ID", "hechun-555")
    monkeypatch.setenv("SUBAGENT", "diabetes-digest-slim")
    provider = LLMProvider(
        type="kimi",
        base_url="https://api.test/v1",
        api_key=SecretStr("test-key"),
    )
    model = LLMModel(provider="kimi", model="kimi-base", max_context_size=4096)

    llm = create_llm(provider, model)
    assert llm is not None
    extra_body = llm.chat_provider.model_parameters.get("extra_body") or {}
    # baseline: user_id + feature, NO turn_id (layered per-turn in _step).
    assert extra_body.get("metadata") == {"user_id": "555", "feature": "digest"}


def test_create_llm_kimi_no_metadata_when_user_id_unset(monkeypatch):
    monkeypatch.delenv("KIMI_USER_ID", raising=False)
    provider = LLMProvider(
        type="kimi",
        base_url="https://api.test/v1",
        api_key=SecretStr("test-key"),
    )
    model = LLMModel(provider="kimi", model="kimi-base", max_context_size=4096)

    llm = create_llm(provider, model)
    assert llm is not None
    extra_body = llm.chat_provider.model_parameters.get("extra_body") or {}
    assert "metadata" not in extra_body


# --------------------------------------------------------------------------- #
# wire schema backward compat (K3-a)
# --------------------------------------------------------------------------- #
def test_prompt_params_turn_id_defaults_none():
    from kimi_cli.wire.jsonrpc import JSONRPCPromptMessage

    # Old frame without turn_id still parses (backward compatible).
    params = JSONRPCPromptMessage.Params.model_validate({"user_input": "hello"})
    assert params.turn_id is None


def test_prompt_params_turn_id_parsed_when_present():
    from kimi_cli.wire.jsonrpc import JSONRPCPromptMessage

    params = JSONRPCPromptMessage.Params.model_validate(
        {"user_input": "hello", "turn_id": "t_abc123"}
    )
    assert params.turn_id == "t_abc123"
