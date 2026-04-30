from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from kimi_cli.memory.entry import MemoryEntry
from kimi_cli.utils.logging import logger


@contextlib.contextmanager
def _locked(path: Path, exclusive: bool) -> Iterator[None]:
    """Acquire an advisory ``fcntl`` lock on a sidecar file.

    The lock is held on a ``<path>.lock`` file rather than on the data file
    itself so the data file can be replaced atomically while the lock is held.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    flag = os.O_RDWR | os.O_CREAT
    fd = os.open(lock_path, flag, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_raw(path: Path) -> list[MemoryEntry]:
    if not path.exists():
        return []
    out: list[MemoryEntry] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(MemoryEntry.model_validate_json(line))
                except Exception as e:
                    logger.warning("Skipping malformed memory line in {p}: {e}", p=path, e=e)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return []
        raise
    return out


def _write_atomic(path: Path, entries: list[MemoryEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(entry.model_dump_json())
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def read_entries(path: Path) -> list[MemoryEntry]:
    """Read all entries from ``path`` under a shared lock."""
    with _locked(path, exclusive=False):
        return _read_raw(path)


def append_entry(path: Path, entry: MemoryEntry) -> None:
    """Append ``entry`` to ``path``."""
    with _locked(path, exclusive=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry.model_dump_json())
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())


def update_entry(path: Path, entry_id: str, content: str) -> MemoryEntry | None:
    """Replace the ``content`` of the entry with the given ID. Returns the
    updated entry, or ``None`` if no such ID exists."""
    with _locked(path, exclusive=True):
        entries = _read_raw(path)
        updated: MemoryEntry | None = None
        for i, e in enumerate(entries):
            if e.id == entry_id:
                entries[i] = e.model_copy(update={"content": content, "updated_at": time.time()})
                updated = entries[i]
                break
        if updated is not None:
            _write_atomic(path, entries)
        return updated


def delete_entry(path: Path, entry_id: str) -> bool:
    """Remove the entry with the given ID. Returns ``True`` if a row was deleted."""
    with _locked(path, exclusive=True):
        entries = _read_raw(path)
        kept = [e for e in entries if e.id != entry_id]
        if len(kept) == len(entries):
            return False
        _write_atomic(path, kept)
        return True
