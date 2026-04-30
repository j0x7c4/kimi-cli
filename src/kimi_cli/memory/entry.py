from __future__ import annotations

import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

MemoryKind = Literal["user", "feedback", "project", "reference"]
MemoryScope = Literal["session", "persistent"]


class MemoryEntry(BaseModel):
    """A single structured memory record.

    Used both inside ``SessionState.session_memory`` (session-scoped notes) and
    inside the user-private ``persistent.jsonl`` file (cross-session memory).
    """

    id: str = Field(default_factory=lambda: uuid4().hex)
    kind: MemoryKind
    scope: MemoryScope
    content: str
    created_at: float = Field(default_factory=time.time)
    updated_at: float | None = None

    def render(self) -> str:
        return f"- [{self.kind}] ({self.id[:8]}) {self.content}"
