import inspect
import json
import os
from pathlib import Path
from typing import Literal, override

from kosong.tooling import BriefDisplayBlock, CallableTool2, ToolError, ToolReturnValue
from pydantic import BaseModel, Field

from kimi_cli.memory import (
    MemoryEntry,
    append_entry,
    delete_entry,
    read_entries,
    update_entry,
)
from kimi_cli.memory.paths import ANONYMOUS_USER_SENTINEL
from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.utils import load_desc
from kimi_cli.utils.logging import logger

NAME = "Memory"

_BASE_DESCRIPTION = load_desc(Path(__file__).parent / "description.md")

ListScope = Literal["session", "persistent", "all"]
WriteScope = Literal["session", "persistent"]
EntryKind = Literal["user", "feedback", "project", "reference"]


class AddOp(BaseModel):
    op: Literal["add"] = "add"
    kind: EntryKind = Field(description="The category of memory being recorded.")
    scope: WriteScope = Field(
        description=(
            "`session` keeps the entry in the current conversation only. "
            "`persistent` writes to the user's cross-session memory."
        ),
    )
    content: str = Field(min_length=1, description="The memory body. Be concise but specific.")


class ListOp(BaseModel):
    op: Literal["list"] = "list"
    scope: ListScope = Field(default="all", description="Which scope(s) to list.")


class UpdateOp(BaseModel):
    op: Literal["update"] = "update"
    id: str = Field(description="The id of the entry to update.")
    content: str = Field(min_length=1, description="The new body for the entry.")


class DeleteOp(BaseModel):
    op: Literal["delete"] = "delete"
    id: str = Field(description="The id of the entry to delete.")


class Params(BaseModel):
    operation: AddOp | ListOp | UpdateOp | DeleteOp = Field(
        discriminator="op",
        description="The memory operation to perform.",
    )


def _ok(output: str, brief: str) -> ToolReturnValue:
    return ToolReturnValue(
        is_error=False,
        output=output,
        message="",
        display=[BriefDisplayBlock(text=brief)],
    )


def _format_entries(entries: list[MemoryEntry], header: str) -> str:
    if not entries:
        return f"{header}: (empty)"
    lines = [f"{header}:"]
    for e in entries:
        lines.append(e.render())
    return "\n".join(lines)


class Memory(CallableTool2[Params]):
    name: str = NAME
    description: str = _BASE_DESCRIPTION
    params: type[Params] = Params

    def __init__(self, runtime: Runtime) -> None:
        super().__init__()
        self._runtime = runtime
        # M4 §2.4.2.C: persistent memory append goes through KimoStorage so
        # KIMI_STORAGE_BACKEND={file,postgres} swap transparently. Lazily
        # constructed on first use so File-mode dev paths pay no cost.
        # File backend keeps writing persistent.jsonl (upstream-compatible);
        # Pg backend writes ai_user_memory + dispatches by owner_id namespace
        # (spec §2.4.2.D: hechun-<bigint> / webui-<uuid>).
        self.__storage_singleton: object | None = None

    def _storage(self):
        from kimi_cli.storage import build_storage

        if self.__storage_singleton is None:
            self.__storage_singleton = build_storage()
        return self.__storage_singleton

    def _resolve_owner_id(self) -> str:
        """Resolve the owner_id namespace for the current session.

        Source precedence (spec §2.4.2.D):
          1. ``KIMI_USER_ID`` env (set by sandbox runner from SessionState.owner_id)
          2. ``session.state.owner_id`` (local-mode CLI)
          3. ``__anonymous__`` sentinel (dev / unauthenticated)
        """
        env_owner = os.environ.get("KIMI_USER_ID")
        if env_owner:
            return env_owner
        state_owner = self._runtime.session.state.owner_id
        if state_owner:
            return state_owner
        return ANONYMOUS_USER_SENTINEL

    @property
    def _persistent_file(self) -> Path:
        return self._runtime.user_memory_dir / "persistent.jsonl"

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        op = params.operation
        if isinstance(op, AddOp):
            return await self._add(op)
        if isinstance(op, ListOp):
            return await self._list(op)
        if isinstance(op, UpdateOp):
            return await self._update(op)
        if isinstance(op, DeleteOp):
            return await self._delete(op)
        return ToolError(message=f"Unknown operation: {op!r}", brief="Bad operation")

    async def _request_persistent_approval(
        self, action: str, description: str
    ) -> ToolReturnValue | None:
        """Gate persistent-memory mutations through user approval.

        Persistent memory survives across sessions and influences the agent's
        future behavior, so the user must opt in. Returns ``None`` when the
        action is approved (continue), or a rejection ``ToolError`` otherwise.
        """
        result = await self._runtime.approval.request(
            self.name,
            f"memory.{action}",
            description,
        )
        if not result:
            return result.rejection_error()
        return None

    async def _add(self, op: AddOp) -> ToolReturnValue:
        if op.scope == "persistent":
            preview = op.content if len(op.content) <= 200 else op.content[:200] + "..."
            rejection = await self._request_persistent_approval(
                "add",
                f"Add persistent memory ({op.kind}): {preview}",
            )
            if rejection is not None:
                return rejection

        entry = MemoryEntry(kind=op.kind, scope=op.scope, content=op.content)
        if op.scope == "session":
            self._runtime.session.state.session_memory.append(entry)
            self._runtime.session.save_state()
        else:
            # M4 §2.4.2.C: persistent path goes through KimoStorage so the
            # file/pg backend swap is transparent (no more direct jsonl
            # append). Memory 不再双写 (jsonl + DB): File mode writes
            # persistent.jsonl, Pg mode writes ai_user_memory — never both.
            #
            # M4 §2.4.2.D: PgKimoStorage uses MemoryEntry.source_kimo_session_id
            # (set via setattr — keeps upstream pydantic schema unchanged)
            # to populate ai_user_memory.source_kimo_session_id.
            self._attach_source_session(entry)
            owner_id = self._resolve_owner_id()
            try:
                storage = self._storage()
                # hechun-fork-cci (方案 B): the CCI worker delegates persistent
                # memory to the gateway over the wire via RemoteKimoStorage,
                # which exposes an async fast-path (a sync call on the loop
                # thread would deadlock on the wire round-trip). Prefer it when
                # present; every other backend keeps the sync path.
                aappend = getattr(storage, "aappend_user_memory", None)
                if inspect.iscoroutinefunction(aappend):
                    await aappend(owner_id, entry)
                else:
                    storage.append_user_memory(owner_id, entry)
            except Exception as e:
                # Spec §5.5 hard contract: memory append must NOT block the
                # LLM stream. Both FileKimoStorage and PgKimoStorage already
                # swallow internally; this extra guard catches the rare case
                # where build_storage() itself raises (bad env at session
                # start) so the Memory tool can still report a soft failure
                # instead of crashing the worker.
                logger.error(
                    "[Memory.add] storage.append_user_memory raised owner_id={oid}: {err}",
                    oid=owner_id,
                    err=e,
                )
        return _ok(
            output=json.dumps({"id": entry.id, "scope": op.scope, "kind": op.kind}),
            brief=f"Remembered ({op.scope}/{op.kind})",
        )

    def _attach_source_session(self, entry: MemoryEntry) -> None:
        """Stamp the current kimo session UUID onto the entry (M4 §2.4.2.D).

        Uses ``setattr`` rather than a model field so we don't touch the
        upstream :class:`MemoryEntry` pydantic schema (Literal kinds,
        round-trip compatibility). :class:`PgKimoStorage` uses ``getattr``
        with a None fallback to read this field.
        """
        try:
            sid = self._runtime.session.id  # str UUID hex
        except Exception:
            return
        if not sid:
            return
        try:
            from uuid import UUID

            sid_uuid = UUID(sid)
            object.__setattr__(entry, "source_kimo_session_id", sid_uuid)
        except (ValueError, TypeError):
            # Session id not a UUID (e.g. tests with synthetic ids) — skip
            # the stamp; PgKimoStorage falls back to NULL gracefully.
            pass

    async def _list(self, op: ListOp) -> ToolReturnValue:
        sections: list[str] = []
        if op.scope in ("session", "all"):
            sections.append(
                _format_entries(
                    list(self._runtime.session.state.session_memory),
                    "Session memory",
                )
            )
        if op.scope in ("persistent", "all"):
            sections.append(
                _format_entries(await self._list_persistent(), "Persistent memory")
            )
        return _ok(output="\n\n".join(sections), brief=f"Listed ({op.scope})")

    async def _list_persistent(self) -> list[MemoryEntry]:
        """Read persistent memory for the current owner.

        hechun-fork-cci (方案 B): under a DB/remote backend, persistent entries
        live in ``ai_user_memory`` (RDS), not ``persistent.jsonl`` — reading the
        file would show nothing. Route through the active storage; the CCI worker
        delegates to the gateway via the async ``alist_user_memory`` fast-path.
        File/dev mode keeps reading ``persistent.jsonl``.
        """
        backend = (os.environ.get("KIMI_STORAGE_BACKEND") or "file").strip().lower()
        if backend in ("postgres", "mysql"):
            owner_id = self._resolve_owner_id()
            try:
                storage = self._storage()
                alist = getattr(storage, "alist_user_memory", None)
                if inspect.iscoroutinefunction(alist):
                    return await alist(owner_id)
                return storage.list_user_memory(owner_id)
            except Exception as e:
                logger.error(
                    "[Memory.list] storage.list_user_memory failed owner_id={oid}: {err}; "
                    "falling back to file",
                    oid=owner_id,
                    err=e,
                )
        return read_entries(self._persistent_file)

    async def _update(self, op: UpdateOp) -> ToolReturnValue:
        # Try session first (cheaper), then persistent.
        for i, entry in enumerate(self._runtime.session.state.session_memory):
            if entry.id == op.id:
                self._runtime.session.state.session_memory[i] = entry.model_copy(
                    update={"content": op.content}
                )
                self._runtime.session.save_state()
                return _ok(
                    output=json.dumps({"id": op.id, "scope": "session"}),
                    brief="Memory updated",
                )
        # Persistent path requires approval.
        preview = op.content if len(op.content) <= 200 else op.content[:200] + "..."
        rejection = await self._request_persistent_approval(
            "update",
            f"Update persistent memory ({op.id[:8]}): {preview}",
        )
        if rejection is not None:
            return rejection
        updated = update_entry(self._persistent_file, op.id, op.content)
        if updated is None:
            return ToolError(message=f"No memory entry with id={op.id!r}.", brief="Not found")
        return _ok(
            output=json.dumps({"id": op.id, "scope": "persistent"}),
            brief="Memory updated",
        )

    async def _delete(self, op: DeleteOp) -> ToolReturnValue:
        before = len(self._runtime.session.state.session_memory)
        self._runtime.session.state.session_memory[:] = [
            e for e in self._runtime.session.state.session_memory if e.id != op.id
        ]
        if len(self._runtime.session.state.session_memory) != before:
            self._runtime.session.save_state()
            return _ok(
                output=json.dumps({"id": op.id, "scope": "session"}),
                brief="Memory deleted",
            )
        # Persistent path requires approval.
        rejection = await self._request_persistent_approval(
            "delete",
            f"Delete persistent memory entry {op.id[:8]}",
        )
        if rejection is not None:
            return rejection
        if delete_entry(self._persistent_file, op.id):
            return _ok(
                output=json.dumps({"id": op.id, "scope": "persistent"}),
                brief="Memory deleted",
            )
        return ToolError(message=f"No memory entry with id={op.id!r}.", brief="Not found")
