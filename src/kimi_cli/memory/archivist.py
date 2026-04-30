"""Extract conversation summaries and persist them as cross-session memory.

Two entry points:

- ``archive_compaction(soul, compaction_result)`` — extract the summary text
  the LLM has just produced for context compaction. No extra LLM call.

- ``archive_on_session_end(soul)`` — last-resort summary at shutdown. Calls the
  shared ``SimpleCompaction`` once on the current context; on any failure falls
  back to a raw text tail.

Both write to ``{user_memory_dir}/recent.jsonl`` with file locking.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from kosong.message import Message

from kimi_cli.memory.recent import (
    RECENT_FILENAME,
    SessionSummary,
    SummaryTrigger,
    append_summary,
)
from kimi_cli.soul.compaction import CompactionResult, SimpleCompaction
from kimi_cli.utils.logging import logger
from kimi_cli.wire.types import TextPart

if TYPE_CHECKING:
    from kimi_cli.soul.kimisoul import KimiSoul


_MIN_HISTORY_FOR_SESSION_END_SUMMARY = 4
RAW_FALLBACK_TAIL_MESSAGES = 6
RAW_FALLBACK_MAX_CHARS = 4_000


def extract_text(messages: Sequence[Message]) -> str:
    """Concatenate text content from messages, skipping internal ``<system>`` markers."""
    out: list[str] = []
    for msg in messages:
        for part in msg.content:
            if not isinstance(part, TextPart):
                continue
            text = part.text
            stripped = text.strip()
            if stripped.startswith("<system>") or stripped.startswith("<system-reminder>"):
                continue
            out.append(text)
    return "\n".join(t for t in (s.strip() for s in out) if t)


def summary_from_compaction_result(result: CompactionResult) -> str:
    """Pull the summary text the compaction LLM produced.

    ``result.messages[0]`` is a synthesized user message whose first content
    part is the ``<system>...compacted...</system>`` marker followed by the
    actual summary text parts.
    """
    if not result.messages:
        return ""
    return extract_text([result.messages[0]])


def raw_tail_summary(history: Sequence[Message]) -> str:
    """Cheap fallback: return the last few messages as plain text."""
    tail = list(history[-RAW_FALLBACK_TAIL_MESSAGES:])
    text = extract_text(tail)
    if len(text) > RAW_FALLBACK_MAX_CHARS:
        text = text[-RAW_FALLBACK_MAX_CHARS:]
    return text


async def _archive(
    soul: KimiSoul,
    summary_text: str,
    trigger: SummaryTrigger,
) -> None:
    summary_text = summary_text.strip()
    if not summary_text:
        logger.debug("archivist: empty summary, skipping ({t})", t=trigger)
        return

    user_memory_dir = soul.runtime.user_memory_dir
    recent_path = user_memory_dir / RECENT_FILENAME

    work_dir_str: str | None = None
    try:
        work_dir_str = str(soul.runtime.session.work_dir)
    except Exception:
        work_dir_str = None

    summary = SessionSummary(
        session_id=soul.runtime.session.id,
        trigger=trigger,
        summary=summary_text,
        work_dir=work_dir_str,
    )
    try:
        append_summary(recent_path, summary)
        logger.debug(
            "archivist: wrote {t} summary for session {s} ({n} chars)",
            t=trigger,
            s=summary.session_id[:8],
            n=len(summary_text),
        )
    except Exception:
        logger.warning("archivist: failed to write summary", exc_info=True)


async def archive_compaction(
    soul: KimiSoul,
    compaction_result: CompactionResult,
) -> None:
    """Archive the summary produced by the most recent context compaction."""
    text = summary_from_compaction_result(compaction_result)
    if not text:
        # Compaction may have been a no-op (too few messages to summarize).
        return
    await _archive(soul, text, "compaction")


async def archive_on_session_end(soul: KimiSoul) -> None:
    """Best-effort summary at shutdown.

    Tries to summarize the current ``soul.context.history`` via
    ``SimpleCompaction``; on any failure (LLM unavailable, timeout, etc.)
    falls back to a raw text tail of the most recent messages so the user
    still gets *something* in their cross-session memory.
    """
    history = list(soul.context.history)
    if len(history) < _MIN_HISTORY_FOR_SESSION_END_SUMMARY:
        return

    summary_text = ""
    llm = soul.runtime.llm
    if llm is not None:
        try:
            compactor = SimpleCompaction()
            result = await compactor.compact(history, llm)
            summary_text = summary_from_compaction_result(result)
        except Exception:
            logger.warning("archivist: session-end LLM summary failed", exc_info=True)

    if not summary_text:
        summary_text = raw_tail_summary(history)

    if summary_text:
        await _archive(soul, summary_text, "session_end")
