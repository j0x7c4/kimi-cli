"""Worker-side storage proxy that delegates persistent memory to the gateway.

# hechun-fork-cci

Root cause (measured, do not re-diagnose): under the CCI spawner the kimi-cli
worker runs in a remote Huawei CCI Pod that is **not** on the RDS-allowed subnet,
so a direct :class:`MyKimoStorage` connection to RDS fails and every
``append_user_memory`` write is silently swallowed — ``ai_user_memory`` stays
empty while the agent believes it remembered something. The *gateway* (on ECS)
**can** reach RDS (it already writes ``kimo_session_state`` there).

方案 B: the worker no longer touches RDS for user memory. Instead
:class:`RemoteKimoStorage` sends a :class:`MemoryOpRequest` over the existing wire
JSON-RPC request channel (the same mechanism :class:`ToolCallRequest` uses). The
gateway intercepts that frame, runs its *own* ``KimoStorage`` (``MyKimoStorage``
on RDS), and replies with a :class:`MemoryOpResult`. See
``web/runner/process.py:_handle_memory_op_request``.

Because the wire round-trip is inherently async and both call sites (the Memory
tool and the cross-session recall injection) run on the soul's event loop, the
delegated ops are exposed as **async** methods (``aappend_user_memory`` /
``alist_user_memory``). Callers ``await`` them when the active storage advertises
them (duck-typed via ``hasattr``); the synchronous ``append_user_memory`` /
``list_user_memory`` remain for Protocol compatibility but only work off the loop
thread (tests).

Only user memory is remoted. Session-state persistence on CCI is handled
elsewhere (gateway writes ``kimo_session_state`` at create time; the worker reads
it back best-effort in ``run_worker``); this proxy's ``*_session_state`` methods
are inert so a stray call can never crash the worker.

Selection: only when ``KIMO_MEMORY_VIA_GATEWAY`` is truthy (set by the gateway
for CCI Pods). Docker/SIT (worker can reach the DB directly) and file/dev mode
keep their existing direct-storage path untouched.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from kimi_cli.memory.entry import MemoryEntry
from kimi_cli.utils.logging import logger

if TYPE_CHECKING:
    from kimi_cli.session_state import SessionState
    from kimi_cli.wire.types import MemoryOpResult

# How long the worker waits for the gateway to answer a memory op before giving
# up. Writes must never block the LLM stream indefinitely (spec §5.5); on
# timeout we swallow (append) or fall back to empty (list).
_MEMORY_OP_TIMEOUT_S = 10.0


class RemoteKimoStorage:
    """Delegates persistent user memory to the gateway over the wire.

    Implements the memory half of the :class:`KimoStorage` Protocol (both the
    sync methods, for Protocol/type compatibility, and async fast-paths the
    call sites prefer); the session-state half is intentionally inert.
    """

    def __init__(self, timeout_s: float = _MEMORY_OP_TIMEOUT_S) -> None:
        self._timeout_s = timeout_s
        logger.info(
            "[RemoteKimoStorage] initialized — persistent memory delegated to gateway "
            "over wire (CCI worker cannot reach RDS directly)"
        )

    # ── user memory: async fast-path (preferred by call sites) ───

    async def aappend_user_memory(self, owner_id: str, entry: MemoryEntry) -> None:
        """Ask the gateway to append one memory row. Swallow all failures (§5.5)."""
        payload = _entry_to_payload(entry)
        result = await self._send("append", owner_id, entry=payload)
        if result is None:
            logger.error(
                "[RemoteKimoStorage] append got no gateway response owner_id={oid} "
                "(wire unavailable / timeout)",
                oid=owner_id,
            )
            return
        if result.ok:
            logger.info(
                "[RemoteKimoStorage] append OK via gateway owner_id={oid} kind={kind}",
                oid=owner_id,
                kind=entry.kind,
            )
        else:
            logger.error(
                "[RemoteKimoStorage] gateway rejected append owner_id={oid}: {err}",
                oid=owner_id,
                err=result.error,
            )

    async def alist_user_memory(self, owner_id: str, limit: int = 200) -> list[MemoryEntry]:
        """Ask the gateway to list memory rows. Return empty on any failure."""
        result = await self._send("list", owner_id, entry=None, limit=limit)
        if result is None or not result.ok:
            logger.warning(
                "[RemoteKimoStorage] list no/failed gateway response owner_id={oid}; empty",
                oid=owner_id,
            )
            return []
        entries = _parse_entries(result.entries)
        logger.info(
            "[RemoteKimoStorage] list got {n} rows via gateway owner_id={oid}",
            n=len(entries),
            oid=owner_id,
        )
        return entries

    async def _send(
        self, op: str, owner_id: str, *, entry: dict | None, limit: int = 200
    ) -> MemoryOpResult | None:
        from kimi_cli.soul import get_wire_or_none
        from kimi_cli.wire.types import MemoryOpRequest

        wire = get_wire_or_none()
        if wire is None:
            logger.error(
                "[RemoteKimoStorage] no wire for memory op={op} owner_id={oid}",
                op=op,
                oid=owner_id,
            )
            return None
        request = MemoryOpRequest(
            id=uuid4().hex,
            op=op,  # type: ignore[arg-type]
            owner_id=owner_id,
            entry=entry,
            limit=limit,
        )
        wire.soul_side.send(request)
        try:
            return await asyncio.wait_for(request.wait(), timeout=self._timeout_s)
        except TimeoutError:
            logger.error(
                "[RemoteKimoStorage] memory op={op} timed out after {t}s owner_id={oid}",
                op=op,
                t=self._timeout_s,
                oid=owner_id,
            )
            return None
        except Exception as e:  # noqa: BLE001 — must not crash the LLM stream (§5.5)
            logger.error(
                "[RemoteKimoStorage] memory op={op} wire error owner_id={oid}: {err}",
                op=op,
                oid=owner_id,
                err=e,
            )
            return None

    # ── user memory: sync Protocol methods (off-loop only, e.g. tests) ───

    def append_user_memory(self, owner_id: str, entry: MemoryEntry) -> None:
        _run_sync(self.aappend_user_memory(owner_id, entry))

    def list_user_memory(self, owner_id: str, limit: int = 200) -> list[MemoryEntry]:
        return _run_sync(self.alist_user_memory(owner_id, limit)) or []

    # ── session state (inert on this proxy — see module docstring) ───

    def load_session_state(self, kimo_session_id: UUID) -> SessionState | None:
        logger.debug(
            "[RemoteKimoStorage] load_session_state inert on proxy (sid={sid}); None",
            sid=kimo_session_id,
        )
        return None

    def save_session_state(
        self, kimo_session_id: UUID, owner_id: str | None, state: SessionState
    ) -> None:
        logger.debug(
            "[RemoteKimoStorage] save_session_state inert on proxy (sid={sid}); no-op",
            sid=kimo_session_id,
        )

    def delete_session_state(self, kimo_session_id: UUID) -> None:
        logger.debug(
            "[RemoteKimoStorage] delete_session_state inert on proxy (sid={sid}); no-op",
            sid=kimo_session_id,
        )


def _entry_to_payload(entry: MemoryEntry) -> dict:
    """Serialize a :class:`MemoryEntry`, carrying the setattr'd source session id.

    ``MemoryEntry`` has no ``source_kimo_session_id`` field; the Memory tool
    stamps it via ``object.__setattr__`` (keeps the upstream pydantic schema
    untouched). We fold it into the JSON payload so the gateway can populate
    ``ai_user_memory.source_kimo_session_id``.
    """
    payload = json.loads(entry.model_dump_json())
    src = getattr(entry, "source_kimo_session_id", None)
    if isinstance(src, UUID):
        payload["source_kimo_session_id"] = str(src)
    elif isinstance(src, str) and src:
        payload["source_kimo_session_id"] = src
    return payload


def _parse_entries(rows: list[dict]) -> list[MemoryEntry]:
    out: list[MemoryEntry] = []
    for raw in rows:
        try:
            out.append(MemoryEntry.model_validate(raw))
        except Exception as e:  # noqa: BLE001
            logger.warning("[RemoteKimoStorage] skip malformed remote memory row: {err}", err=e)
    return out


def _run_sync(coro):
    """Drive an async storage op from a *synchronous* off-loop caller (tests).

    Refuses to run on the event-loop thread — the async fast-path
    (``aappend_user_memory`` / ``alist_user_memory``) must be used there to avoid
    a deadlock.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError(
        "RemoteKimoStorage sync method called on the event-loop thread; use the "
        "async entrypoint (aappend_user_memory / alist_user_memory)"
    )


__all__ = ["RemoteKimoStorage"]
