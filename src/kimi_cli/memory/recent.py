from __future__ import annotations

import contextlib
import errno
import os
import tempfile
import time
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from kimi_cli.memory.storage import _locked
from kimi_cli.utils.logging import logger

RECENT_FILENAME = "recent.jsonl"

# Cap kept summaries per user. Cross-session injection only reads the tail —
# everything beyond this is dropped on append.
DEFAULT_MAX_SUMMARIES = 20

SummaryTrigger = Literal["compaction", "session_end", "manual"]


class SessionSummary(BaseModel):
    """A condensed record of a past conversation.

    Written by the archivist whenever context is compacted or a session ends.
    Read back by ``CrossSessionMemoryInjectionProvider`` to give the agent a
    short reminder of what happened in prior conversations.
    """

    id: str = Field(default_factory=lambda: uuid4().hex)
    session_id: str
    created_at: float = Field(default_factory=time.time)
    trigger: SummaryTrigger
    summary: str
    # Optional: short title hint or work-dir tag for future filtering.
    work_dir: str | None = None

    def render(self) -> str:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.created_at))
        head = f"[{ts}] (session {self.session_id[:8]}, {self.trigger})"
        return f"{head}\n{self.summary.strip()}"


def _read_raw(path: Path) -> list[SessionSummary]:
    if not path.exists():
        return []
    out: list[SessionSummary] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(SessionSummary.model_validate_json(line))
                except Exception as e:
                    logger.warning("Skipping malformed summary line in {p}: {e}", p=path, e=e)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return []
        raise
    return out


def _write_atomic(path: Path, items: list[SessionSummary]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for s in items:
                f.write(s.model_dump_json())
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def read_recent_summaries(path: Path, *, limit: int | None = None) -> list[SessionSummary]:
    """Return summaries (oldest first). Pass ``limit`` to take only the tail."""
    with _locked(path, exclusive=False):
        items = _read_raw(path)
    if limit is not None and limit > 0:
        items = items[-limit:]
    return items


def append_summary(
    path: Path,
    summary: SessionSummary,
    *,
    max_kept: int = DEFAULT_MAX_SUMMARIES,
) -> None:
    """Append ``summary`` and trim the oldest entries if over ``max_kept``."""
    with _locked(path, exclusive=True):
        items = _read_raw(path)
        items.append(summary)
        if len(items) > max_kept:
            items = items[-max_kept:]
            _write_atomic(path, items)
            return
        # Fast path: no trim needed, just append.
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(summary.model_dump_json())
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())


def trim_old_summaries(path: Path, *, max_kept: int = DEFAULT_MAX_SUMMARIES) -> int:
    """Trim ``path`` so it holds at most ``max_kept`` summaries. Returns dropped count."""
    with _locked(path, exclusive=True):
        items = _read_raw(path)
        if len(items) <= max_kept:
            return 0
        dropped = len(items) - max_kept
        _write_atomic(path, items[-max_kept:])
        return dropped
