from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from kosong.message import Message

from kimi_cli.memory.entry import MemoryEntry
from kimi_cli.memory.recent import (
    RECENT_FILENAME,
    SessionSummary,
    read_recent_summaries,
)
from kimi_cli.memory.storage import read_entries
from kimi_cli.soul.dynamic_injection import DynamicInjection, DynamicInjectionProvider
from kimi_cli.utils.logging import logger

if TYPE_CHECKING:
    from kimi_cli.soul.kimisoul import KimiSoul

_INJECTION_TYPE = "cross_session_memory"
_PERSISTENT_FILENAME = "persistent.jsonl"

# How many recent summaries to surface to the LLM at startup.
_RECENT_INJECTION_LIMIT = 5


class CrossSessionMemoryInjectionProvider(DynamicInjectionProvider):
    """One-shot startup injection of the user's cross-session memory.

    Reads ``persistent.jsonl`` (Memory tool entries) and ``recent.jsonl``
    (archived past-session summaries) once on the first LLM step and caches
    the rendered injection. Subsequent steps return ``[]`` so we don't pay
    file I/O on every step or invalidate prompt cache mid-session.
    """

    def __init__(self) -> None:
        self._injected: bool = False
        self._cached: list[DynamicInjection] = []

    def invalidate(self) -> None:
        """Force a re-read on the next ``get_injections`` call."""
        self._injected = False
        self._cached = []

    async def get_injections(
        self,
        history: Sequence[Message],
        soul: KimiSoul,
    ) -> list[DynamicInjection]:
        if self._injected:
            return self._cached

        self._injected = True
        try:
            user_memory_dir = soul.runtime.user_memory_dir
            # M4 §2.4.2.C/D: under postgres backend, persistent entries live in
            # ai_user_memory (PG table), NOT persistent.jsonl. file path read
            # would return empty / stale → LLM has no recall of past sessions.
            # Read via storage abstraction; file path stays as fallback for
            # file-mode + upstream test harnesses.
            persistent = _read_persistent(user_memory_dir)
            recent = read_recent_summaries(
                user_memory_dir / RECENT_FILENAME,
                limit=_RECENT_INJECTION_LIMIT,
            )
        except Exception:
            logger.warning("cross-session memory read failed", exc_info=True)
            return []

        rendered = _render(persistent, recent)
        if not rendered:
            return []

        self._cached = [DynamicInjection(type=_INJECTION_TYPE, content=rendered)]
        return self._cached


def _read_persistent(user_memory_dir) -> Sequence[MemoryEntry]:
    """Read persistent memory entries — storage-aware (M4 §2.4.2.C/D).

    Lookup order:
      1. Active ``KimoStorage.list_user_memory(owner_id)`` when
         ``KIMI_STORAGE_BACKEND=postgres``; owner_id derived from path:
         user_memory_dir layout is ``{share}/users/{owner_id}/memory/`` so
         the second-to-last component is the owner_id namespace
         (``hechun-<bigint>`` / ``webui-<uuid>`` / ``__anonymous__``).
      2. File fallback ``persistent.jsonl`` (file mode + upstream).
    """
    import os as _os

    if _os.environ.get("KIMI_STORAGE_BACKEND", "file").lower() == "postgres":
        try:
            owner_id = user_memory_dir.parent.name  # users/<owner_id>/memory
        except Exception:
            owner_id = None
        if owner_id:
            try:
                from kimi_cli.storage import build_storage

                entries = build_storage().list_user_memory(owner_id)
                if entries:
                    return entries
            except Exception as _e:  # noqa: BLE001
                logger.warning(
                    "[cross_session_memory] storage probe failed owner_id={oid} "
                    "err={err}; falling back to file",
                    oid=owner_id,
                    err=_e,
                )

    return read_entries(user_memory_dir / _PERSISTENT_FILENAME)


def _render(
    persistent: Sequence[MemoryEntry],
    recent: Sequence[SessionSummary],
) -> str:
    sections: list[str] = []

    if persistent:
        lines = [
            "## Persistent memory",
            "Stable facts/preferences you've recorded across sessions:",
            "",
        ]
        for e in persistent:
            lines.append(e.render())
        sections.append("\n".join(lines))

    if recent:
        lines = [
            "## Recent session summaries",
            "Condensed records of recent past conversations (oldest first):",
            "",
        ]
        for s in recent:
            lines.append(s.render())
            lines.append("")
        sections.append("\n".join(lines).rstrip())

    if not sections:
        return ""

    header = (
        "Cross-session memory — a snapshot of what you knew at the start of "
        "this conversation. Trust the live conversation over this snapshot if "
        "they conflict."
    )
    return header + "\n\n" + "\n\n".join(sections)
