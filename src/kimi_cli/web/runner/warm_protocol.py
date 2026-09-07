"""Warm-pool handshake protocol — the ONLY wire spoken to a pre-warmed worker.

# hechun-fork-cci (warm pool, spec 2026-09-07-sandbox-warmpool-design.md §4/§8.3)

A pre-warmed sandbox worker has完成 static preparation (interpreter boot, heavy
imports, gateway asset download) but has **no soul yet** — ``KimiCLI.create`` and
everything below it (agent spec, toolset, MCP clients) is deliberately deferred
until a real user claims the Pod, because the MCP config substitutes
``${KIMI_USER_ID}`` from ``os.environ`` at build time and a connection built with
an empty identity would never be rebound (spec §4.4 — this is a **security**
constraint, not a latency trade-off).

Consequences that shape this protocol:

* **No JSON-RPC is possible before bind.** ``wire/jsonrpc.py`` speaks to a
  ``WireServer`` wrapped around a soul; in the warm phase nothing would answer,
  and the wire protocol has no ``ping`` method anyway (``JSONRPC_IN_METHODS``
  has none). So liveness probing needs its own frames — they live here.
* **Frames must be unambiguously distinguishable from JSON-RPC**, because the
  gateway writes the client's ``initialize`` frame into the very same stdin pipe
  immediately after the handshake completes. Every warm frame carries a
  top-level ``"kimo_warm"`` discriminator and never a ``"jsonrpc"``/``"method"``
  key; :func:`is_warm_frame` is the single decision point for both sides.

Framing: one JSON object per line, UTF-8, ``\\n``-terminated.

    gateway → worker   bind, ping
    worker  → gateway  ready, pong, bound, error

Ordering (hard requirement — spec §4.2 / W0 spike finding #3): the gateway must
``attach`` → optionally ``ping`` → write ``bind`` → await ``bound`` → only THEN
replay ``initialize``. Replaying initialize first would have it consumed as the
bind line by a worker blocked on the handshake read.
"""

from __future__ import annotations

import json
from typing import Any, cast
from uuid import UUID

#: Protocol version; bumped only on an incompatible frame change. Both sides
#: reject a mismatching major version rather than guessing.
WARM_PROTOCOL_VERSION = 1

#: Discriminator key present on EVERY warm frame and on no JSON-RPC frame.
WARM_KEY = "kimo_warm"

# Frame types.
FRAME_READY = "ready"  # worker → gateway: static prep finished (ok true/false)
FRAME_PING = "ping"  # gateway → worker
FRAME_PONG = "pong"  # worker  → gateway
FRAME_BIND = "bind"  # gateway → worker: claim this Pod for a real session
FRAME_BOUND = "bound"  # worker  → gateway: bind accepted, soul boot starting
FRAME_ERROR = "error"  # worker  → gateway: fatal, this Pod is unusable

#: ``ready`` / ``error`` reason labels (stable — the gateway logs + metrics use them).
REASON_ASSETS_MISSING = "assets_missing"
REASON_AGENT_UNRESOLVED = "agent_unresolved"
REASON_BIND_INVALID = "bind_invalid"
REASON_PREPARE_FAILED = "prepare_failed"

#: Worker exit code for "a bind frame arrived but was unusable". Distinct from
#: ``AGENT_LOAD_FAILURE_EXIT_CODE`` (42) so the gateway can tell "this claim was
#: malformed" from "this Pod cannot load its agent".
WARM_BIND_FAILURE_EXIT_CODE = 43

#: Worker exit code for "static preparation did not produce a usable Pod"
#: (assets never landed / agent yaml unresolvable). Reported in a ``ready`` frame
#: with ``ok: false`` first, so the gateway can mark the row dead with a reason
#: even if it misses the exit code.
WARM_PREPARE_FAILURE_EXIT_CODE = 44

#: env keys a bind frame is allowed to set inside the worker process. A bind
#: frame is gateway-authored, but keeping this an explicit allowlist means a
#: future bug (or a confused-deputy write into the exec stream) cannot inject
#: arbitrary environment — notably not proxy/credential vars — into a process
#: that is about to build MCP connections.
BIND_ENV_ALLOWLIST = frozenset(
    {
        "KIMI_SESSION_ID",
        "KIMI_USER_ID",
        "KIMO_DEFAULT_YOLO",
        "SUBAGENT",
        "KIMI_REQUIRE_AGENT",
        "KIMI_WORK_DIR",
    }
)


class WarmProtocolError(ValueError):
    """A warm frame was malformed / semantically invalid.

    Carries a stable ``reason`` label so the receiver can log + report it
    without re-deriving the classification from the message text.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def encode(frame_type: str, **fields: Any) -> str:
    """Serialise one warm frame to a newline-terminated JSON line."""
    payload: dict[str, Any] = {WARM_KEY: frame_type, "v": WARM_PROTOCOL_VERSION}
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=False) + "\n"


def is_warm_frame(obj: object) -> bool:
    """Whether a decoded JSON value is a warm frame (vs a JSON-RPC frame).

    The single decision point shared by both sides: warm frames are dicts with a
    string ``kimo_warm`` and NO ``jsonrpc`` key. Anything else (including a
    JSON-RPC ``initialize`` that raced ahead) is explicitly not ours.
    """
    if not isinstance(obj, dict):
        return False
    frame = cast("dict[str, Any]", obj)
    kind: Any = frame.get(WARM_KEY)
    return isinstance(kind, str) and "jsonrpc" not in frame


def decode(line: str | bytes) -> dict[str, Any] | None:
    """Decode one line into a warm frame dict, or ``None`` if it isn't one.

    Never raises on malformed input — a non-JSON or non-warm line is simply not
    a warm frame, and the caller decides what to do with it.
    """
    if isinstance(line, (bytes, bytearray)):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        obj: Any = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not is_warm_frame(obj):
        return None
    frame: dict[str, Any] = obj
    return frame


class BindRequest:
    """Validated payload of a ``bind`` frame.

    ``owner_id`` and ``yolo`` are **required** and hard-validated here: on CCI
    the worker's storage is a ``RemoteKimoStorage`` whose ``load_session_state``
    is inert (returns None always), so the bind frame is the ONLY source of
    truth for them (spec §4.3). A bind that omits either one must fail the claim
    — silently defaulting ``yolo`` to False would leave tool approval waiting on
    a UI that has no approval affordance (permanent hang).
    """

    __slots__ = ("session_id", "owner_id", "yolo", "env")

    def __init__(self, session_id: UUID, owner_id: str, yolo: bool, env: dict[str, str]) -> None:
        self.session_id = session_id
        self.owner_id = owner_id
        self.yolo = yolo
        self.env = env

    @classmethod
    def parse(cls, frame: dict[str, Any]) -> BindRequest:
        """Validate a decoded ``bind`` frame; raise :class:`WarmProtocolError`."""
        if frame.get(WARM_KEY) != FRAME_BIND:
            raise WarmProtocolError(REASON_BIND_INVALID, f"not a bind frame: {frame!r}")
        if frame.get("v") != WARM_PROTOCOL_VERSION:
            raise WarmProtocolError(
                REASON_BIND_INVALID, f"protocol version mismatch: {frame.get('v')!r}"
            )
        raw_sid = frame.get("session_id")
        if not isinstance(raw_sid, str) or not raw_sid:
            raise WarmProtocolError(REASON_BIND_INVALID, "session_id missing")
        try:
            session_id = UUID(raw_sid)
        except ValueError as e:
            raise WarmProtocolError(
                REASON_BIND_INVALID, f"session_id not a UUID: {raw_sid!r}"
            ) from e
        owner_id = frame.get("owner_id")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise WarmProtocolError(REASON_BIND_INVALID, "owner_id missing")
        yolo = frame.get("yolo")
        if not isinstance(yolo, bool):
            raise WarmProtocolError(REASON_BIND_INVALID, f"yolo not a bool: {yolo!r}")
        raw_env: Any = frame.get("env") or {}
        if not isinstance(raw_env, dict):
            raise WarmProtocolError(REASON_BIND_INVALID, "env not an object")
        raw_env_typed = cast("dict[Any, Any]", raw_env)
        env = {
            k: str(v)
            for k, v in raw_env_typed.items()
            if isinstance(k, str) and k in BIND_ENV_ALLOWLIST
        }
        return cls(session_id=session_id, owner_id=owner_id.strip(), yolo=yolo, env=env)

    def to_frame(self) -> str:
        """Serialise back to the wire (used by the gateway to send a claim)."""
        return encode(
            FRAME_BIND,
            session_id=str(self.session_id),
            owner_id=self.owner_id,
            yolo=self.yolo,
            env=dict(self.env),
        )


__all__ = [
    "BIND_ENV_ALLOWLIST",
    "FRAME_BIND",
    "FRAME_BOUND",
    "FRAME_ERROR",
    "FRAME_PING",
    "FRAME_PONG",
    "FRAME_READY",
    "REASON_AGENT_UNRESOLVED",
    "REASON_ASSETS_MISSING",
    "REASON_BIND_INVALID",
    "REASON_PREPARE_FAILED",
    "WARM_KEY",
    "WARM_PROTOCOL_VERSION",
    "WARM_BIND_FAILURE_EXIT_CODE",
    "WARM_PREPARE_FAILURE_EXIT_CODE",
    "BindRequest",
    "WarmProtocolError",
    "decode",
    "encode",
    "is_warm_frame",
]
