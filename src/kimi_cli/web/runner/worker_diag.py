"""Worker-side runtime diagnostics: forward Pod logs to the gateway.

# hechun-fork-cci

Why this exists: on CCI the worker runs inside a Pod whose ``kimi.log`` nobody
reads. ``enable_logging`` adds a **file sink only**, and fd 2 is dup2'd into
that same file, so every ``logger.warning`` the runtime emits — MCP tool call
timed out, MCP tool returned error, approval request timed out — dies inside the
Pod. The gateway sees a session that simply stopped producing output.

That blind spot has now cost us three incidents (2026-09-08 being the third: a
session sat at ``state=busy`` for five minutes and the only way to learn
anything was to guess from the *absence* of outbound connections — the worker
itself never said a word, and the手工 ``/stop`` used to unblock the user
destroyed the evidence).

The transport is the one channel that reaches the gateway live: a single-line
JSON frame on stdout carrying ``kimo_diag`` (:func:`kimi_cli.web.runner.process
._diag_frame` consumes it, logs it, and — importantly — does **not** broadcast
it to WebSocket clients, nor feed it to the JSON-RPC validator).

Deliberate constraints, none of them optional:

* **Off unless ``KIMO_WORKER_TRACE`` is truthy.** A forwarder that is on by
  default would ship Pod-internal text to gateway logs in every environment.
  The env must also be in ``container.py:_SANDBOX_ENV_VARS`` or it never
  reaches the Pod — that exact "配置写了却没生效" trap already bit
  ``KIMO_WORKER_TIMING`` (2026-09-07).
* **Never raises.** Diagnostics that can break the worker are worse than no
  diagnostics.
* **Never recurses.** The sink writes to stdout directly; it must not log.
* **Bounded.** Messages are truncated and the rate is capped, because stdout is
  shared with the JSON-RPC wire: a log storm would compete with real frames.
  Dropped frames are counted and reported, so a gap is visible rather than
  silent.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any

#: Env gate. Truthy → forward. Must also be in ``_SANDBOX_ENV_VARS``.
ENV_TRACE = "KIMO_WORKER_TRACE"

#: Minimum loguru level forwarded (name, e.g. ``WARNING``). Anything below stays
#: in ``kimi.log``. WARNING is the useful default: the MCP timeout / MCP error /
#: approval timeout paths all log at WARNING or ERROR.
ENV_TRACE_LEVEL = "KIMO_WORKER_TRACE_LEVEL"
DEFAULT_LEVEL = "WARNING"

#: Frame key the gateway matches on (mirrors ``process._DIAG_FRAME_KEY``).
FRAME_KEY = "kimo_diag"

#: Identity carried on every frame. Read from the environment **at emit time**,
#: never cached: on the warm-pool path the worker starts identity-free and both
#: values are written into ``os.environ`` only when the bind frame arrives
#: (``worker.py`` ``_apply_bound_identity``). Caching at import would stamp every
#: frame of a warm-pooled session as anonymous.
#:
#: Both are worth carrying even though the gateway already prefixes ``sid=``:
#: the gateway does not know the user, and a Pod-side log without a user id is
#: nearly useless once more than one person is on the system.
_ENV_SESSION_ID = "KIMI_SESSION_ID"
_ENV_USER_ID = "KIMI_USER_ID"

#: Per-message truncation. Tracebacks are the reason this is not smaller.
MAX_TEXT = 2000

#: Rate cap: at most this many frames per window, then drop and count.
_RATE_MAX = 20
_RATE_WINDOW_S = 10.0


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def enabled() -> bool:
    return _truthy(os.environ.get(ENV_TRACE))


class _RateLimiter:
    """Fixed-window cap. Counts what it drops so gaps are never silent."""

    def __init__(self, limit: int = _RATE_MAX, window_s: float = _RATE_WINDOW_S) -> None:
        self._limit = limit
        self._window_s = window_s
        self._lock = threading.Lock()
        self._window_start = 0.0
        self._sent = 0
        self._dropped = 0

    def take(self, now: float) -> tuple[bool, int]:
        """Return ``(allowed, dropped_since_last_allowed)``."""
        with self._lock:
            if now - self._window_start >= self._window_s:
                self._window_start = now
                self._sent = 0
            if self._sent < self._limit:
                self._sent += 1
                dropped, self._dropped = self._dropped, 0
                return True, dropped
            self._dropped += 1
            return False, 0


_limiter = _RateLimiter()
#: Guards against re-entrancy: a sink that logged would feed itself.
_in_emit = threading.local()


def emit(kind: str, **fields: Any) -> None:
    """Write one diagnostic frame to stdout. Never raises, never recurses."""
    if not enabled():
        return
    if getattr(_in_emit, "active", False):
        return
    _in_emit.active = True
    try:
        allowed, dropped = _limiter.take(time.monotonic())
        if not allowed:
            return
        payload: dict[str, Any] = {FRAME_KEY: kind}
        # Identity first so it survives truncation of anything downstream, and
        # so a frame is greppable by user even when the message itself is cut.
        sid = os.environ.get(_ENV_SESSION_ID)
        uid = os.environ.get(_ENV_USER_ID)
        if sid:
            payload["sid"] = sid
        if uid:
            payload["uid"] = uid
        if dropped:
            # Report the gap on the next frame that gets through, so a rate cap
            # never looks like "nothing happened".
            payload["dropped"] = dropped
        for key, value in fields.items():
            payload[key] = _clip(value)
        _emit_line(json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001 — diagnostics must never break the worker
        pass
    finally:
        _in_emit.active = False


def _emit_line(line: str) -> None:
    """The ONE place that touches the wire.

    stdout is shared with JSON-RPC, so every diagnostic byte goes through here:
    one flushed line, no partial writes, nothing else in the module allowed to
    write. Kept as a seam so tests can drive the re-entrancy path without
    fighting pytest's stdout capture.
    """
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + f"…(+{len(value) - MAX_TEXT} chars)"
    return value


def install_log_forwarding(logger: Any) -> bool:
    """Add a loguru sink forwarding records to the gateway. Returns whether it was added.

    Level comes from ``KIMO_WORKER_TRACE_LEVEL`` (default ``WARNING``). The sink
    carries module/function/line so a forwarded warning can be traced back to
    source without opening the Pod.
    """
    if not enabled():
        return False

    level = (os.environ.get(ENV_TRACE_LEVEL) or DEFAULT_LEVEL).strip().upper() or DEFAULT_LEVEL

    def _sink(message: Any) -> None:
        try:
            record = message.record
            emit(
                "worker_log",
                level=record["level"].name,
                where=f"{record['name']}:{record['function']}:{record['line']}",
                text=record["message"],
                exc=str(record["exception"].value) if record.get("exception") else None,
            )
        except Exception:  # noqa: BLE001 — a broken sink must not break logging
            pass

    try:
        logger.add(_sink, level=level, format="{message}", enqueue=False)
    except Exception:  # noqa: BLE001
        return False
    return True


__all__ = [
    "DEFAULT_LEVEL",
    "ENV_TRACE",
    "ENV_TRACE_LEVEL",
    "FRAME_KEY",
    "emit",
    "enabled",
    "install_log_forwarding",
]
