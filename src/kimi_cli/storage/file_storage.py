"""File-backed implementation of :class:`KimoStorage` (spec §2.4.2.C).

Thin wrapper around upstream helpers — preserves filesystem layout and
upstream-compatible behaviour. Selected when ``KIMI_STORAGE_BACKEND=file``
(default).

Layout (unchanged from upstream):

- ``{share_dir}/sessions/{wd_hash}/{kimo_session_id}/state.json``
- ``{share_dir}/users/{owner_id}/memory/persistent.jsonl``

Session-dir resolution mirrors :func:`kimi_cli.web.api.sessions.load_session_by_id`:
iterate :class:`Metadata.work_dirs`; for each ``wd`` look at
``wd.sessions_dir / str(kimo_session_id)``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import UUID

from kimi_cli.memory.entry import MemoryEntry
from kimi_cli.memory.paths import get_persistent_memory_file
from kimi_cli.memory.storage import append_entry, read_entries
from kimi_cli.metadata import load_metadata
from kimi_cli.session_state import (
    SessionState,
    load_session_state as _load_state_from_dir,
    save_session_state as _save_state_to_dir,
)
from kimi_cli.utils.logging import logger


class FileKimoStorage:
    """Filesystem-backed storage. Upstream-compatible."""

    # ─── session state ───

    def load_session_state(self, kimo_session_id: UUID) -> SessionState | None:
        session_dir = self._resolve_session_dir(kimo_session_id)
        if session_dir is None:
            return None
        return _load_state_from_dir(session_dir)

    def save_session_state(
        self, kimo_session_id: UUID, owner_id: str | None, state: SessionState
    ) -> None:
        session_dir = self._resolve_session_dir(kimo_session_id, create_if_missing=True)
        if session_dir is None:
            logger.warning(
                "[FileKimoStorage] save_session_state: cannot resolve session_dir sid={sid}",
                sid=kimo_session_id,
            )
            return
        # Carry owner_id through SessionState (V30 column source-of-truth; file backend
        # still keeps it on the model for round-trip parity with pg backend).
        if owner_id is not None and state.owner_id != owner_id:
            state.owner_id = owner_id
        _save_state_to_dir(state, session_dir)

    def delete_session_state(self, kimo_session_id: UUID) -> None:
        session_dir = self._resolve_session_dir(kimo_session_id)
        if session_dir is None or not session_dir.is_dir():
            return
        # Only purge the per-session directory — do NOT remove the per-work-dir
        # parent (other sessions may live there).
        try:
            shutil.rmtree(session_dir)
        except OSError as e:
            logger.warning(
                "[FileKimoStorage] delete_session_state rmtree failed sid={sid}: {err}",
                sid=kimo_session_id,
                err=e,
            )

    # ─── user memory ───

    def append_user_memory(self, owner_id: str, entry: MemoryEntry) -> None:
        path = get_persistent_memory_file(owner_id)
        try:
            append_entry(path, entry)
        except OSError as e:
            # Same fallback contract as the pg backend: memory append must not
            # block the LLM stream (spec §5.5).
            logger.warning(
                "[FileKimoStorage] append_user_memory failed owner_id={oid}: {err}",
                oid=owner_id,
                err=e,
            )

    def list_user_memory(self, owner_id: str, limit: int = 200) -> list[MemoryEntry]:
        path = get_persistent_memory_file(owner_id)
        entries = read_entries(path)
        if limit <= 0:
            return []
        # Most-recent-first to match the pg backend ordering.
        return list(reversed(entries))[:limit]

    # ─── internals ───

    def _resolve_session_dir(
        self, kimo_session_id: UUID, *, create_if_missing: bool = False
    ) -> Path | None:
        """Locate the on-disk session dir for ``kimo_session_id``.

        Mirrors :func:`kimi_cli.web.api.sessions.load_session_by_id`: scans
        ``metadata.work_dirs`` for ``wd.sessions_dir/{uuid}``. Returns the first
        match where ``state.json`` exists; with ``create_if_missing=True`` also
        returns a directory whose work-dir hash exists (for saves on fresh
        sessions).
        """
        sid_str = str(kimo_session_id)
        metadata = load_metadata()
        # First pass: prefer a dir that already has state.json (load path).
        for wd in metadata.work_dirs:
            candidate = wd.sessions_dir / sid_str
            if (candidate / "state.json").exists():
                return candidate
        if not create_if_missing:
            # Loose fallback: any wd whose session dir exists at all
            for wd in metadata.work_dirs:
                candidate = wd.sessions_dir / sid_str
                if candidate.is_dir():
                    return candidate
            return None
        # save path: if not yet on disk, use the first wd known to metadata.
        # This matches upstream behaviour where the runner creates the dir on
        # first activity. If no work_dirs at all, give up (we don't invent one).
        if not metadata.work_dirs:
            return None
        candidate = metadata.work_dirs[0].sessions_dir / sid_str
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate


__all__ = ["FileKimoStorage"]
