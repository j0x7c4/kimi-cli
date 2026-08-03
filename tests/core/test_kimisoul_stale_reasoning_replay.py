"""Reproduces the "hot-turn never terminates" bug for reasoning models.

Root cause under test
---------------------
Assistant messages keep their ``ThinkPart`` in history, and both the ``kimi``
and ``openai_legacy`` chat providers re-serialize that ``ThinkPart`` back into
the ``reasoning_content`` field of *every* subsequent request
(``kimi.py:_convert_message`` / ``openai_legacy.py:_convert_message``).

For a reasoning model reached through litellp/DashScope (deepseek-v4-flash),
replaying the reasoning of an *already completed* prior turn poisons the next
request: the model re-enters/continues reasoning and the stream never reaches a
clean ``finish_reason: stop`` with visible text — the turn cannot terminate.

This asymmetry is exactly what the backend observes:
- Cold turn (first prompt, no prior assistant reasoning in history) -> clean end.
- Hot turn (second+ prompt in a reused session, history now carries the prior
  turn's reasoning) -> never terminates.
- With thinking disabled (no ``reasoning_content`` ever produced) -> no stale
  reasoning to replay -> bug does not appear.

The fake provider below models the degeneracy deterministically: if the request
history replays reasoning from a *completed* turn (an assistant ``ThinkPart``
that sits before the last user message) it returns a think-only stream — which
``kosong.generate`` surfaces as ``APIEmptyResponseError`` (the "abnormal
termination" guard) instead of a clean turn. That is the observable stand-in for
the production stream-stall.

The termination detection itself (kosong's stream-end / think-only guard, the
soul turn/step machinery, TurnEnd emission) is NOT mocked — only the upstream
model behaviour is scripted.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Self

import pytest
from kosong.chat_provider import StreamedMessagePart, ThinkingEffort, TokenUsage
from kosong.message import Message, TextPart, ThinkPart
from kosong.tooling import Tool
from kosong.tooling.simple import SimpleToolset

from kimi_cli.llm import LLM
from kimi_cli.soul import run_soul
from kimi_cli.soul.agent import Agent, Runtime
from kimi_cli.soul.context import Context
from kimi_cli.soul.kimisoul import KimiSoul


class _StaticStream:
    def __init__(self, parts: Sequence[StreamedMessagePart]) -> None:
        self._iter = self._gen(parts)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> StreamedMessagePart:
        return await self._iter.__anext__()

    async def _gen(
        self, parts: Sequence[StreamedMessagePart]
    ) -> AsyncIterator[StreamedMessagePart]:
        for part in parts:
            yield part

    @property
    def id(self) -> str | None:
        return "deepseek-like"

    @property
    def usage(self) -> TokenUsage | None:
        return None


def _replays_completed_turn_reasoning(history: Sequence[Message]) -> bool:
    """True if the request replays reasoning from an already-completed turn.

    A completed-turn assistant message is any assistant message that appears
    *before* the last user message and still carries a ``ThinkPart``. Reasoning
    for the current in-flight turn (after the last user message, e.g. tool-call
    continuation) is legitimate and intentionally ignored here.
    """
    last_user_idx = -1
    for i, msg in enumerate(history):
        if msg.role == "user":
            last_user_idx = i
    for msg in history[:last_user_idx]:
        if msg.role == "assistant" and any(isinstance(p, ThinkPart) for p in msg.content):
            return True
    return False


class DeepSeekLikeProvider:
    """A reasoning model that degenerates when fed stale (completed-turn) reasoning."""

    name = "deepseek-like"

    def __init__(self) -> None:
        self.generate_attempts = 0
        self.poisoned_requests = 0

    @property
    def model_name(self) -> str:
        return "deepseek-like"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return "low"

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> _StaticStream:
        self.generate_attempts += 1
        if _replays_completed_turn_reasoning(history):
            # Poisoned: the model keeps reasoning and never emits usable text.
            # kosong.generate() surfaces this as APIEmptyResponseError (think-only).
            self.poisoned_requests += 1
            return _StaticStream(
                [ThinkPart(think="...still reasoning about the replayed trace...")]
            )
        # Clean request: think, then a real answer, stream ends normally.
        return _StaticStream(
            [
                ThinkPart(think="brief reasoning"),
                TextPart(text=f"answer #{self.generate_attempts}"),
            ]
        )

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


def _make_soul(runtime: Runtime, llm: LLM, tmp_path: Path) -> tuple[KimiSoul, Context]:
    from dataclasses import replace

    agent = Agent(
        name="Digest Agent",
        system_prompt="Digest test prompt.",
        toolset=SimpleToolset(),
        runtime=replace(runtime, llm=llm),
    )
    context = Context(file_backend=tmp_path / "history.jsonl")
    return KimiSoul(agent, context=context), context


async def _drain(wire) -> None:  # noqa: ANN001
    from kimi_cli.utils.aioqueue import QueueShutDown

    ui = wire.ui_side(merge=True)
    while True:
        try:
            await ui.receive()
        except QueueShutDown:
            return


@pytest.mark.asyncio
async def test_reused_session_second_turn_terminates(runtime: Runtime, tmp_path: Path) -> None:
    """A reused session's second turn must complete instead of hanging.

    Cold turn (turn 1) works. The hot turn (turn 2) replays turn 1's reasoning
    into the request; a reasoning model degenerates on that stale trace and the
    turn never reaches a clean end. RED until stale cross-turn reasoning is
    stripped from the request history.
    """
    # Single attempt so the failing path resolves fast (no long backoff).
    runtime.config.loop_control.max_retries_per_step = 1
    provider = DeepSeekLikeProvider()
    llm = LLM(chat_provider=provider, max_context_size=100_000, capabilities=set())
    soul, context = _make_soul(runtime, llm, tmp_path)

    # Cold turn — first prompt of a fresh session.
    await run_soul(soul, "first prompt", _drain, asyncio.Event())
    assert context.history[-1].extract_text(" ").strip() == "answer #1"

    # Hot turn — second prompt in the SAME (reused) session. Must terminate.
    await run_soul(soul, "follow-up prompt", _drain, asyncio.Event())

    assert provider.poisoned_requests == 0, (
        "turn 2 request replayed the completed turn's reasoning_content"
    )
    assert context.history[-1].extract_text(" ").strip() == "answer #2"
