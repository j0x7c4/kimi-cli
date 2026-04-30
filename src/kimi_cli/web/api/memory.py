"""Memory API: knowledge base (shared) + persistent / session memory + recent summaries.

Authorization model:

- ``GET`` on knowledge files is allowed for any authenticated user that has
  access to the session's work_dir (same rule as session read-access).
- ``PUT`` / ``DELETE`` on knowledge files require the caller to own the
  session (or be admin). Knowledge is shared across users; only owners /
  admins may modify the project knowledge.
- All persistent-memory operations are scoped to the *caller's own* user
  directory. Anonymous (static-token-only) callers operate on the
  ``__anonymous__`` bucket.
- ``GET /recent`` is read-only and scoped to the caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from kosong.message import Message
from pydantic import BaseModel, Field, ValidationError

from kimi_cli.memory import (
    RECENT_FILENAME,
    MemoryEntry,
    SessionSummary,
    append_entry,
    append_summary,
    delete_entry,
    get_knowledge_dir,
    get_persistent_memory_file,
    get_user_memory_dir,
    read_entries,
    read_recent_summaries,
    resolve_owner_id,
    update_entry,
)
from kimi_cli.memory.archivist import (
    raw_tail_summary,
    summary_from_compaction_result,
)
from kimi_cli.memory.entry import MemoryKind
from kimi_cli.utils.logging import logger
from kimi_cli.web.store.sessions import load_session_by_id
from kimi_cli.web.user_auth import (
    get_current_user,
    require_current_user,
    require_current_user_sse,
)

router = APIRouter(prefix="/api/memory", tags=["memory"])


# ----------------------- helpers -----------------------

# Knowledge filenames are constrained to a small alphabet so we can join them
# under the knowledge dir without worrying about traversal.
_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.md$")


def _validate_kb_filename(name: str) -> str:
    if not _FILENAME_RE.match(name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid knowledge filename. Allowed: [A-Za-z0-9._-]+.md",
        )
    if name.startswith("."):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Knowledge filenames may not start with a dot.",
        )
    return name


def _resolve_session_work_dir(session_id: UUID) -> Path:
    session = load_session_by_id(session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return Path(session.work_dir)


def _check_session_owner(session_id: UUID, user: dict[str, Any]) -> None:
    """Block KB writes by users who don't own this session unless they are admin."""
    session = load_session_by_id(session_id)
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if user.get("role") == "admin":
        return
    owner_id = getattr(session, "owner_id", None)
    if owner_id is None:
        # Pre-multi-user / anonymous sessions: any logged-in user may edit.
        return
    if owner_id != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not session owner")


def _caller_owner_id(user: dict[str, Any] | None) -> str:
    if user is None:
        return resolve_owner_id(None)
    return resolve_owner_id(user.get("id"))


# ----------------------- knowledge base -----------------------


class KnowledgeFile(BaseModel):
    name: str
    size: int
    mtime: float


class KnowledgeFileContent(BaseModel):
    name: str
    content: str


class KnowledgeWriteRequest(BaseModel):
    content: str = Field(description="The new full content of the markdown file.")


@router.get("/knowledge", summary="List knowledge base files")
async def list_knowledge(
    session_id: UUID,
    _user: dict[str, Any] = Depends(require_current_user),  # noqa: B008
) -> list[KnowledgeFile]:
    work_dir = _resolve_session_work_dir(session_id)
    knowledge_dir = get_knowledge_dir(work_dir)
    if not knowledge_dir.exists():
        return []
    out: list[KnowledgeFile] = []
    for p in sorted(knowledge_dir.glob("*.md")):
        if not p.is_file():
            continue
        st = p.stat()
        out.append(KnowledgeFile(name=p.name, size=st.st_size, mtime=st.st_mtime))
    return out


@router.get("/knowledge/{filename}", summary="Read a knowledge base file")
async def read_knowledge_file(
    filename: str,
    session_id: UUID,
    _user: dict[str, Any] = Depends(require_current_user),  # noqa: B008
) -> KnowledgeFileContent:
    name = _validate_kb_filename(filename)
    work_dir = _resolve_session_work_dir(session_id)
    path = get_knowledge_dir(work_dir) / name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return KnowledgeFileContent(name=name, content=path.read_text(encoding="utf-8"))


@router.put("/knowledge/{filename}", summary="Create or update a knowledge base file")
async def write_knowledge_file(
    filename: str,
    session_id: UUID,
    body: KnowledgeWriteRequest,
    user: dict[str, Any] = Depends(require_current_user),  # noqa: B008
) -> KnowledgeFile:
    name = _validate_kb_filename(filename)
    _check_session_owner(session_id, user)
    work_dir = _resolve_session_work_dir(session_id)
    knowledge_dir = get_knowledge_dir(work_dir)
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    path = knowledge_dir / name
    path.write_text(body.content, encoding="utf-8")
    st = path.stat()
    return KnowledgeFile(name=name, size=st.st_size, mtime=st.st_mtime)


@router.delete(
    "/knowledge/{filename}",
    summary="Delete a knowledge base file",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_knowledge_file(
    filename: str,
    session_id: UUID,
    user: dict[str, Any] = Depends(require_current_user),  # noqa: B008
) -> None:
    name = _validate_kb_filename(filename)
    _check_session_owner(session_id, user)
    work_dir = _resolve_session_work_dir(session_id)
    path = get_knowledge_dir(work_dir) / name
    if not path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    path.unlink()


# ----------------------- persistent memory (per-user) -----------------------


class PersistentEntry(BaseModel):
    id: str
    kind: MemoryKind
    content: str
    created_at: float
    updated_at: float | None = None

    @classmethod
    def from_entry(cls, e: MemoryEntry) -> PersistentEntry:
        return cls(
            id=e.id,
            kind=e.kind,
            content=e.content,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )


class PersistentAddRequest(BaseModel):
    kind: MemoryKind
    content: str = Field(min_length=1)


class PersistentUpdateRequest(BaseModel):
    content: str = Field(min_length=1)


@router.get("/persistent", summary="List the caller's persistent memory entries")
async def list_persistent(
    user: dict[str, Any] | None = Depends(get_current_user),  # noqa: B008
) -> list[PersistentEntry]:
    path = get_persistent_memory_file(_caller_owner_id(user))
    return [PersistentEntry.from_entry(e) for e in read_entries(path)]


@router.post("/persistent", summary="Add a persistent memory entry", status_code=201)
async def add_persistent(
    body: PersistentAddRequest,
    user: dict[str, Any] | None = Depends(get_current_user),  # noqa: B008
) -> PersistentEntry:
    path = get_persistent_memory_file(_caller_owner_id(user))
    entry = MemoryEntry(kind=body.kind, scope="persistent", content=body.content)
    append_entry(path, entry)
    return PersistentEntry.from_entry(entry)


@router.put("/persistent/{entry_id}", summary="Update a persistent memory entry")
async def update_persistent(
    entry_id: str,
    body: PersistentUpdateRequest,
    user: dict[str, Any] | None = Depends(get_current_user),  # noqa: B008
) -> PersistentEntry:
    path = get_persistent_memory_file(_caller_owner_id(user))
    updated = update_entry(path, entry_id, body.content)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")
    return PersistentEntry.from_entry(updated)


@router.delete(
    "/persistent/{entry_id}",
    summary="Delete a persistent memory entry",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_persistent(
    entry_id: str,
    user: dict[str, Any] | None = Depends(get_current_user),  # noqa: B008
) -> None:
    path = get_persistent_memory_file(_caller_owner_id(user))
    if not delete_entry(path, entry_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")


# ----------------------- recent summaries (read-only) -----------------------


class RecentSummaryView(BaseModel):
    id: str
    session_id: str
    created_at: float
    trigger: str
    summary: str
    work_dir: str | None = None

    @classmethod
    def from_summary(cls, s: SessionSummary) -> RecentSummaryView:
        return cls(
            id=s.id,
            session_id=s.session_id,
            created_at=s.created_at,
            trigger=s.trigger,
            summary=s.summary,
            work_dir=s.work_dir,
        )


@router.get("/recent", summary="List the caller's recent session summaries")
async def list_recent(
    limit: int = 50,
    user: dict[str, Any] | None = Depends(get_current_user),  # noqa: B008
) -> list[RecentSummaryView]:
    if limit <= 0:
        limit = 50
    if limit > 200:
        limit = 200
    path = get_user_memory_dir(_caller_owner_id(user)) / RECENT_FILENAME
    summaries = read_recent_summaries(path, limit=limit)
    return [RecentSummaryView.from_summary(s) for s in summaries]


# ----------------------- on-demand archive -----------------------

# Special "role" markers used in context.jsonl that are not real Messages.
_CONTEXT_NON_MESSAGE_ROLES = frozenset({"_system_prompt", "_usage", "_checkpoint"})

# Need at least this many messages in history before a manual summary is worth
# producing — anything shorter is just the raw turn(s) verbatim.
_MIN_HISTORY_FOR_MANUAL_ARCHIVE = 2


def _load_context_messages(path: Path) -> list[Message]:
    """Read ``context.jsonl`` and return the validated ``Message`` records.

    Skips internal ``_system_prompt`` / ``_usage`` / ``_checkpoint`` records and
    silently drops malformed lines (logged at warning level).
    """
    if not path.is_file():
        return []
    out: list[Message] = []
    with open(path, encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line, strict=False)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "Skipping malformed context line {n} in {p}: {e}",
                    n=line_no,
                    p=path,
                    e=exc,
                )
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("role") in _CONTEXT_NON_MESSAGE_ROLES:
                continue
            try:
                out.append(Message.model_validate(obj))
            except ValidationError as exc:
                logger.warning(
                    "Skipping invalid context message line {n} in {p}: {e}",
                    n=line_no,
                    p=path,
                    e=exc,
                )
    return out


async def _summarize_via_llm(history: list[Message]) -> str:
    """Run a one-shot SimpleCompaction over ``history`` and return summary text.

    Constructs an LLM from the configured default provider/model. Returns ""
    on any failure or if no default LLM is configured — caller falls back to
    ``raw_tail_summary``.
    """
    from kimi_cli.auth.oauth import OAuthManager
    from kimi_cli.config import load_config
    from kimi_cli.llm import create_llm
    from kimi_cli.soul.compaction import SimpleCompaction

    config = load_config()
    model_name = config.default_model
    if not model_name or model_name not in config.models:
        return ""
    model_config = config.models[model_name]
    provider_config = config.providers.get(model_config.provider)
    if provider_config is None:
        return ""

    oauth = OAuthManager(config)
    try:
        await oauth.ensure_fresh()
    except Exception:
        logger.warning("memory archive: oauth refresh failed", exc_info=True)

    llm = create_llm(provider_config, model_config, oauth=oauth)
    if llm is None:
        return ""
    try:
        result = await SimpleCompaction().compact(history, llm)
        return summary_from_compaction_result(result)
    except Exception:
        logger.warning("memory archive: LLM summary failed", exc_info=True)
        return ""


class ArchiveAccepted(BaseModel):
    session_id: str
    status: str = Field(default="queued")


class MemoryEventBus:
    """In-process pub/sub for per-user memory events.

    Multi-tab safe (a user can have multiple SSE connections; each is its own
    queue). Not durable across server restarts — frontend has a safety timeout
    that reconciles stuck in-flight states.
    """

    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}

    def subscribe(self, owner_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        self._subs.setdefault(owner_id, []).append(queue)
        return queue

    def unsubscribe(self, owner_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        queues = self._subs.get(owner_id)
        if not queues:
            return
        with contextlib.suppress(ValueError):
            queues.remove(queue)
        if not queues:
            self._subs.pop(owner_id, None)

    def publish(self, owner_id: str, event: dict[str, Any]) -> None:
        for queue in list(self._subs.get(owner_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("memory event bus: dropped event for {o} (queue full)", o=owner_id)


_BUS = MemoryEventBus()


async def _run_archive_in_background(
    session_id: UUID,
    owner_id: str,
    history: list[Message],
    work_dir: str | None,
) -> None:
    """Summarize ``history`` and append the result to the user's recent.jsonl.

    Always publishes either an ``archive.completed`` or ``archive.failed`` event
    to the user's SSE bus. Never raises.
    """
    try:
        summary_text = (await _summarize_via_llm(history)).strip()
        if not summary_text:
            summary_text = raw_tail_summary(history).strip()
        if not summary_text:
            raise RuntimeError("Failed to produce any summary text")

        summary = SessionSummary(
            session_id=str(session_id),
            trigger="manual",
            summary=summary_text,
            work_dir=work_dir,
        )
        recent_path = get_user_memory_dir(owner_id) / RECENT_FILENAME
        append_summary(recent_path, summary)
        _BUS.publish(
            owner_id,
            {
                "type": "archive.completed",
                "session_id": str(session_id),
                "summary": RecentSummaryView.from_summary(summary).model_dump(mode="json"),
            },
        )
    except Exception as exc:
        logger.warning("memory archive: background task failed", exc_info=True)
        _BUS.publish(
            owner_id,
            {
                "type": "archive.failed",
                "session_id": str(session_id),
                "error": str(exc) or "Archive failed",
            },
        )


@router.post(
    "/sessions/{session_id}/archive",
    summary="Queue a summary of the session in the background",
    status_code=status.HTTP_202_ACCEPTED,
)
async def archive_session(
    session_id: UUID,
    user: dict[str, Any] = Depends(require_current_user),  # noqa: B008
) -> ArchiveAccepted:
    """Kick off a manual archive job and return immediately.

    Validation (existence, ownership, minimum history) runs synchronously so
    4xx errors surface in the original HTTP response. The actual LLM
    summarization runs in :func:`_run_archive_in_background`; completion or
    failure is pushed to the caller's SSE channel at ``GET /api/memory/events``.
    """
    session = load_session_by_id(session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Session not found")
    _check_session_owner(session_id, user)

    history = _load_context_messages(session.kimi_cli_session.context_file)
    if len(history) < _MIN_HISTORY_FOR_MANUAL_ARCHIVE:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Session has too little conversation history to summarize",
        )

    work_dir_str: str | None = None
    try:
        work_dir_str = str(session.kimi_cli_session.work_dir)
    except Exception:
        work_dir_str = None

    owner_id = _caller_owner_id(user)
    asyncio.create_task(_run_archive_in_background(session_id, owner_id, history, work_dir_str))
    return ArchiveAccepted(session_id=str(session_id))


@router.get("/events", summary="Stream memory events for the caller")
async def memory_events(
    user: dict[str, Any] = Depends(require_current_user_sse),  # noqa: B008
) -> StreamingResponse:
    """Server-Sent Events stream of memory events for the calling user.

    Currently emits ``archive.completed`` and ``archive.failed`` events for
    manual archive jobs the caller has started. Auto-archives that happen in
    the worker process are not pushed here (different process); the frontend
    discovers them by polling ``GET /api/memory/recent``.

    Authentication: prefer cookie ``kimi_session``; falls back to ``?token=``
    query param so browser ``EventSource`` (which cannot send headers) works.
    """
    owner_id = _caller_owner_id(user)
    queue = _BUS.subscribe(owner_id)

    async def gen():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            _BUS.unsubscribe(owner_id, queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
