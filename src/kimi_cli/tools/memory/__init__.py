import json
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
from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.utils import load_desc

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

    @property
    def _persistent_file(self) -> Path:
        return self._runtime.user_memory_dir / "persistent.jsonl"

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        op = params.operation
        if isinstance(op, AddOp):
            return await self._add(op)
        if isinstance(op, ListOp):
            return self._list(op)
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
            append_entry(self._persistent_file, entry)
        return _ok(
            output=json.dumps({"id": entry.id, "scope": op.scope, "kind": op.kind}),
            brief=f"Remembered ({op.scope}/{op.kind})",
        )

    def _list(self, op: ListOp) -> ToolReturnValue:
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
                _format_entries(read_entries(self._persistent_file), "Persistent memory")
            )
        return _ok(output="\n\n".join(sections), brief=f"Listed ({op.scope})")

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
