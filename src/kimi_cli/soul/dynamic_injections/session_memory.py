from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from kosong.message import Message

from kimi_cli.memory.entry import MemoryEntry
from kimi_cli.soul.dynamic_injection import DynamicInjection, DynamicInjectionProvider

if TYPE_CHECKING:
    from kimi_cli.soul.kimisoul import KimiSoul

_INJECTION_TYPE = "session_memory"


class SessionMemoryInjectionProvider(DynamicInjectionProvider):
    """Surfaces ``SessionState.session_memory`` to the LLM as a system-reminder.

    Re-injects only when the set of entries (or their content) has changed
    since the previous injection. This avoids spamming the conversation when
    the agent never touched the Memory tool, while still keeping the model
    in sync with newly-added notes.
    """

    def __init__(self) -> None:
        self._last_signature: str | None = None

    async def get_injections(
        self,
        history: Sequence[Message],
        soul: KimiSoul,
    ) -> list[DynamicInjection]:
        entries: list[MemoryEntry] = list(soul.runtime.session.state.session_memory)
        signature = _signature(entries)

        # First pass with no entries -> nothing to inject, but lock in the
        # signature so the agent isn't reminded "memory is empty" each step.
        if not entries:
            self._last_signature = signature
            return []

        if signature == self._last_signature:
            return []
        self._last_signature = signature

        return [DynamicInjection(type=_INJECTION_TYPE, content=_render(entries))]


def _signature(entries: Sequence[MemoryEntry]) -> str:
    if not entries:
        return ""
    parts = [f"{e.id}:{hash(e.content)}" for e in entries]
    return "|".join(parts)


def _render(entries: Sequence[MemoryEntry]) -> str:
    lines = [
        "Session memory (in-conversation notes you've recorded via the Memory tool):",
        "",
    ]
    for e in entries:
        lines.append(e.render())
    lines.extend(
        [
            "",
            "Treat these as authoritative facts/preferences for the current session.",
            "Update or remove them with the Memory tool when they change.",
        ]
    )
    return "\n".join(lines)
