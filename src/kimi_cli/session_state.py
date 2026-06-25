from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError

from kimi_cli.memory.entry import MemoryEntry
from kimi_cli.utils.io import atomic_json_write
from kimi_cli.utils.logging import logger

if TYPE_CHECKING:
    from kimi_cli.storage import KimoStorage

STATE_FILE_NAME = "state.json"


class ApprovalStateData(BaseModel):
    yolo: bool = False
    afk: bool = False
    auto_approve_actions: set[str] = Field(default_factory=set)


class TodoItemState(BaseModel):
    """A single todo item stored in session or subagent state."""

    title: str
    status: Literal["pending", "in_progress", "done"]


class SessionState(BaseModel):
    version: int = 1
    approval: ApprovalStateData = Field(default_factory=ApprovalStateData)
    additional_dirs: list[str] = Field(default_factory=list)
    custom_title: str | None = None
    title_generated: bool = False
    title_generate_attempts: int = 0
    plan_mode: bool = False
    plan_session_id: str | None = None
    plan_slug: str | None = None
    # Archive state (previously in metadata.json)
    wire_mtime: float | None = None
    archived: bool = False
    archived_at: float | None = None
    auto_archive_exempt: bool = False
    # Todo list state
    todos: list[TodoItemState] = Field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]
    # Multi-user ownership
    owner_id: str | None = None  # user ID of the user who created this session
    # Session-scoped memory (Layer 3 of the three-layer memory system).
    # Cleared when the session ends; persistent memory lives outside SessionState.
    session_memory: list[MemoryEntry] = Field(default_factory=list)  # pyright: ignore[reportUnknownVariableType]


_LEGACY_METADATA_FILENAME = "metadata.json"


def _migrate_legacy_metadata(session_dir: Path, state: SessionState) -> str:
    """Migrate fields from legacy metadata.json into SessionState.

    Returns:
        "migrated" - fields were merged into state, caller should save and delete legacy file
        "no_change" - legacy file parsed but no fields needed, caller can delete legacy file
        "skip" - legacy file missing or unreadable, caller should not touch it
    """
    metadata_file = session_dir / _LEGACY_METADATA_FILENAME
    if not metadata_file.exists():
        return "skip"
    try:
        data = json.loads(metadata_file.read_text(encoding="utf-8"))
    except Exception:
        # Leave the file intact for future retry — it may be temporarily unreadable
        return "skip"

    changed = False

    # Migrate title fields (only if state has defaults)
    if state.custom_title is None and data.get("title") and data["title"] != "Untitled":
        state.custom_title = data["title"]
        changed = True
    if not state.title_generated and data.get("title_generated"):
        state.title_generated = True
        changed = True
    if state.title_generate_attempts == 0 and data.get("title_generate_attempts", 0) > 0:
        state.title_generate_attempts = data["title_generate_attempts"]
        changed = True

    # Migrate archive fields
    if not state.archived and data.get("archived"):
        state.archived = True
        changed = True
    if state.archived_at is None and data.get("archived_at") is not None:
        state.archived_at = data["archived_at"]
        changed = True
    if not state.auto_archive_exempt and data.get("auto_archive_exempt"):
        state.auto_archive_exempt = True
        changed = True

    # Migrate wire_mtime
    if state.wire_mtime is None and data.get("wire_mtime") is not None:
        state.wire_mtime = data["wire_mtime"]
        changed = True

    return "migrated" if changed else "no_change"


def load_session_state(session_dir: Path) -> SessionState:
    # M4 §2.4.2.C / J: under a DB backend (postgres OR mysql) the file
    # ``state.json`` is **stale** (sessions.py:create_session writes owner_id /
    # approval.yolo via storage only, not back to disk). All callers — including
    # KimiSession.__init__ inside the sandbox — must see the DB row as source of
    # truth, otherwise approval.yolo / owner_id stays False / None and
    # Memory.add(persistent) blocks on approval forever (踩过 2026-06-04，see
    # feedback_kimo_owner_id_disk_stale_under_pg_backend).
    #
    # 2026-06-25: kimo 已迁 mysql（spec 2026-06-24 §8）→ 这里必须把 mysql 也算 DB
    # 后端，否则 CCI worker 起 fresh session 时读不到 DB 里的 yolo → iOS Memory 卡死。
    #
    # Strategy: under a DB backend, probe storage first using the UUID embedded in
    # ``session_dir.name``. Storage miss / parse error → fall back to file path so
    # file-mode + older callsites keep working bit-for-bit.
    import os as _os

    if _os.environ.get("KIMI_STORAGE_BACKEND", "file").lower() in ("postgres", "mysql"):
        try:
            kimo_session_id = UUID(session_dir.name)
        except (ValueError, AttributeError):
            kimo_session_id = None
        if kimo_session_id is not None:
            try:
                from kimi_cli.storage import build_storage

                _storage = build_storage()
                _state = _storage.load_session_state(kimo_session_id)
                if _state is not None:
                    return _state
            except Exception as _e:  # noqa: BLE001
                logger.warning(
                    "[load_session_state] storage probe failed sid={sid} err={err}; "
                    "falling back to file",
                    sid=kimo_session_id,
                    err=_e,
                )

    state_file = session_dir / STATE_FILE_NAME
    if not state_file.exists():
        state = SessionState()
    else:
        try:
            with open(state_file, encoding="utf-8") as f:
                state = SessionState.model_validate(json.load(f))
        except (json.JSONDecodeError, ValidationError, UnicodeDecodeError) as e:
            logger.warning("Corrupted state file, using defaults: {path}", path=state_file)
            from kimi_cli.telemetry import track

            track("session_load_failed", reason=type(e).__name__)
            state = SessionState()

    # One-time migration from legacy metadata.json (best-effort)
    migration = _migrate_legacy_metadata(session_dir, state)
    if migration in ("migrated", "no_change"):
        try:
            if migration == "migrated":
                save_session_state(state, session_dir)
            (session_dir / _LEGACY_METADATA_FILENAME).unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "Failed to persist migration for {path}, will retry next load",
                path=session_dir,
            )

    return state


def save_session_state(state: SessionState, session_dir: Path) -> None:
    state_file = session_dir / STATE_FILE_NAME
    atomic_json_write(state.model_dump(mode="json"), state_file)


# ─── M4 storage-aware helpers (spec §2.4.2.C) ─────────────────────────────
# Parallel API to ``load_session_state`` / ``save_session_state`` that routes
# through a :class:`kimi_cli.storage.KimoStorage` instance instead of touching
# the on-disk session_dir directly. New callers (sessions.py M4 path) should
# prefer these; existing callers passing ``session_dir: Path`` keep working.
#
# Under ``KIMI_STORAGE_BACKEND=file`` these are bit-for-bit equivalent to the
# Path-based helpers above (the FileKimoStorage backend delegates back into
# them). Under ``KIMI_STORAGE_BACKEND=postgres`` the state lives in the
# ``kimo_session_state`` table (hechun-backend Flyway V30).


def load_session_state_via_storage(
    kimo_session_id: UUID, storage: KimoStorage
) -> SessionState:
    """Load session state via the configured storage backend.

    Returns a fresh :class:`SessionState` (upstream default) when the backend
    has no record for ``kimo_session_id`` — same fallback contract as the
    Path-based ``load_session_state``.
    """
    loaded = storage.load_session_state(kimo_session_id)
    if loaded is None:
        return SessionState()
    return loaded


def save_session_state_via_storage(
    state: SessionState,
    kimo_session_id: UUID,
    storage: KimoStorage,
    *,
    owner_id: str | None = None,
) -> None:
    """Save session state via the configured storage backend.

    ``owner_id`` defaults to ``state.owner_id`` — passing it explicitly lets
    callers (e.g. sessions.create_session) overwrite the field at save time
    when resolving the three-priority namespace (spec §2.4.2.D).
    """
    resolved_owner = owner_id if owner_id is not None else state.owner_id
    storage.save_session_state(kimo_session_id, resolved_owner, state)
