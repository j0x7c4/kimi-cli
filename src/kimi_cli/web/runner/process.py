"""Session process management for Kimi CLI web interface."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import mimetypes
import os
import sys
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from kosong.message import ContentPart, ImageURLPart, TextPart
from PIL import Image
from PIL.Image import Image as PILImage
from pydantic import TypeAdapter
from starlette.websockets import WebSocket, WebSocketState

from kimi_cli import logger

try:
    from pillow_heif import register_heif_opener  # type: ignore[import-not-found]

    register_heif_opener()
except Exception:  # pragma: no cover - degraded mode if pillow-heif missing
    logger.debug("pillow-heif not available; uploaded HEIC/HEIF will fail to decode")
from kimi_cli.config import LLMModel, load_config
from kimi_cli.llm import ModelCapability, derive_model_capabilities
from kimi_cli.utils.subprocess_env import get_clean_env
from kimi_cli.web.models import (
    SessionNoticeEvent,
    SessionNoticePayload,
    SessionState,
    SessionStatus,
)
from kimi_cli.web.runner.messages import new_session_status_message
from kimi_cli.web.store.sessions import load_session_by_id
from kimi_cli.wire.jsonrpc import (
    JSONRPCCancelMessage,
    JSONRPCErrorObject,
    JSONRPCErrorResponse,
    JSONRPCEventMessage,
    JSONRPCInMessage,
    JSONRPCInMessageAdapter,
    JSONRPCOutMessage,
    JSONRPCPromptMessage,
    JSONRPCRequestMessage,
    JSONRPCSuccessResponse,
)
from kimi_cli.wire.serde import deserialize_wire_message

JSONRPCOutMessageAdapter = TypeAdapter[JSONRPCOutMessage](JSONRPCOutMessage)


def _is_initialize_frame(message: str) -> bool:
    """Cheap check whether a raw JSON-RPC frame is an ``initialize`` request.

    hechun-fork-cci. Used by :meth:`SessionProcess.send_message` to remember the
    client's capability handshake so it can be replayed to workers that restart.
    Tolerates malformed input (returns False) — the full validation happens later
    in ``send_message`` for frames we actually act on.
    """
    try:
        obj = json.loads(message)
    except (ValueError, TypeError):
        return False
    return isinstance(obj, dict) and obj.get("method") == "initialize"


class SessionProcess:
    """Manages a single session's KimiCLI subprocess.

    Handles:
    - Starting/stopping the subprocess
    - Reading from stdout (wire messages from KimiCLI)
    - Writing to stdin (user input to KimiCLI)
    - Broadcasting messages to connected WebSockets

    Concurrency model:
    - `SessionProcess` is the long-lived container for a `session_id`.
      It may outlive worker restarts.
    - Liveness vs busy are separate:
      - `is_alive` / `is_running`: worker subprocess exists and has not exited.
      - `is_busy`: there is at least one in-flight prompt id.
    - WebSocket fanout supports "join while running":
      - New clients replay `wire.jsonl` history first.
      - Live messages during replay are buffered per-WS and flushed afterwards.

    Locks:
    - `_lock` guards worker lifecycle and busy state.
    - `_ws_lock` guards WebSocket state.
    """

    def __init__(self, session_id: UUID) -> None:
        """Initialize a session process."""
        self.session_id = session_id
        self._in_flight_prompt_ids: set[str] = set()
        self._status_seq = 0
        self._worker_id: str | None = None
        self._status = SessionStatus(
            session_id=self.session_id,
            state="stopped",
            seq=self._status_seq,
            worker_id=self._worker_id,
            reason=None,
            detail=None,
            updated_at=datetime.now(UTC),
        )
        self._process: asyncio.subprocess.Process | None = None
        self._websockets: set[WebSocket] = set()
        self._websocket_count = 0
        self._replay_buffers: dict[WebSocket, list[str]] = {}
        self._read_task: asyncio.Task[None] | None = None
        self._expecting_exit = False
        self._lock = asyncio.Lock()
        self._ws_lock = asyncio.Lock()
        self._sent_files: set[str] = set()
        # hechun-fork-cci: the last ``initialize`` frame the gateway forwarded for
        # this session, verbatim (already newline-free JSON string). The gateway is
        # otherwise a stateless byte-pipe for wire frames — it does NOT synthesize
        # ``initialize`` itself; the backend sends it over the WebSocket. But a CCI
        # worker restarts every few minutes (Pod recycle / exec drop) and each fresh
        # worker boots with ``_initialized=False`` / ``_client_supports_question=False``,
        # so capability-gated tools (AskUserQuestion, plan mode) stay HIDDEN unless
        # the client re-declares. Relying on the backend to re-send initialize on
        # every restart is racy: if a prompt reaches the new worker first it starts a
        # turn and the later initialize is rejected with "An agent turn is already in
        # progress" (wire/server.py). So we remember the frame here and re-inject it
        # as the FIRST thing written to every freshly-spawned worker, before any
        # prompt — order-guaranteed and restart-proof, independent of backend timing.
        self._last_initialize_frame: str | None = None
        # monotonic timestamp of the last activity on this session
        # (worker start / prompt sent / busy transition). The CCI idle sweeper
        # (web/app.py) reclaims sessions whose worker is alive but idle past
        # KIMI_CCI_IDLE_TTL_SECONDS. Harmless on docker/local (just a timestamp,
        # never read there). None until the first activity.
        self._last_active_at: float | None = None

    @property
    def last_active_at(self) -> float | None:
        """time.monotonic() of the last activity, or None if never active."""
        return self._last_active_at

    def _touch_active(self) -> None:
        """Mark this session as active *now* (monotonic clock)."""
        self._last_active_at = time.monotonic()

    @property
    def is_alive(self) -> bool:
        """Whether the worker subprocess exists and has not exited."""
        process = self._process
        return process is not None and process.returncode is None

    @property
    def is_running(self) -> bool:
        """Backward-compatible name: indicates worker liveness."""
        return self.is_alive

    @property
    def is_busy(self) -> bool:
        """Whether the session is currently processing a prompt."""
        return len(self._in_flight_prompt_ids) > 0

    def clear_in_flight(self) -> None:
        """Clear stale in-flight prompt IDs (e.g. after an error)."""
        self._in_flight_prompt_ids.clear()

    @property
    def status(self) -> SessionStatus:
        """Current runtime status snapshot."""
        return self._status

    @property
    def websocket_count(self) -> int:
        """Get the number of connected WebSockets."""
        return self._websocket_count

    async def send_status_snapshot(self, ws: WebSocket) -> None:
        """Send the current status snapshot to a specific WebSocket."""
        await ws.send_text(new_session_status_message(self._status).model_dump_json())

    def _build_status(
        self,
        state: SessionState,
        reason: str | None,
        detail: str | None,
    ) -> SessionStatus | None:
        """Build a new status object if different from current."""
        current = self._status
        if (
            current.state == state
            and current.reason == reason
            and current.detail == detail
            and current.worker_id == self._worker_id
        ):
            return None
        self._status_seq += 1
        status = SessionStatus(
            session_id=self.session_id,
            state=state,
            seq=self._status_seq,
            worker_id=self._worker_id,
            reason=reason,
            detail=detail,
            updated_at=datetime.now(UTC),
        )
        self._status = status
        return status

    async def _emit_status(
        self,
        state: SessionState,
        *,
        reason: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Emit a status update if different from current."""
        # hechun-fork-cci: a busy transition is activity (prompt in flight) — bump
        # the idle clock so the CCI sweeper never reclaims a working session.
        if state == "busy":
            self._touch_active()
        status = self._build_status(state, reason, detail)
        if status is None:
            return
        await self._broadcast(new_session_status_message(status).model_dump_json())

    async def start(
        self,
        *,
        reason: str | None = None,
        detail: str | None = None,
        restart_started_at: float | None = None,
    ) -> None:
        """Start the KimiCLI subprocess."""
        async with self._lock:
            if self.is_alive:
                if self._read_task is None or self._read_task.done():
                    self._read_task = asyncio.create_task(self._read_loop())
                return

            self._in_flight_prompt_ids.clear()
            self._expecting_exit = False
            self._worker_id = str(uuid4())
            self._touch_active()

            # 16MB buffer for large messages (e.g., base64-encoded images)
            STREAM_LIMIT = 16 * 1024 * 1024

            if getattr(sys, "frozen", False):
                worker_cmd = [sys.executable, "__web-worker", str(self.session_id)]
            else:
                worker_cmd = [
                    sys.executable,
                    "-m",
                    "kimi_cli.web.runner.worker",
                    str(self.session_id),
                ]

            self._process = await asyncio.create_subprocess_exec(
                *worker_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=STREAM_LIMIT,
                env=get_clean_env(),
            )

            self._read_task = asyncio.create_task(self._read_loop())
            # hechun-fork-cci: a fresh worker was just spawned. Re-declare the client's
            # capabilities to it FIRST (before any prompt) so capability-gated tools
            # survive worker restarts. No-op until an initialize frame has been seen.
            await self._replay_initialize_to_worker()
            if restart_started_at is not None:
                elapsed_ms = int((time.perf_counter() - restart_started_at) * 1000)
                detail = f"restart_ms={elapsed_ms}"
                await self._emit_status("idle", reason=reason or "start", detail=detail)
                await self._emit_restart_notice(reason=reason, restart_ms=elapsed_ms)
            else:
                await self._emit_status("idle", reason=reason or "start", detail=None)

    async def _replay_initialize_to_worker(self) -> None:
        """Re-inject the remembered ``initialize`` frame into a freshly-spawned worker.

        hechun-fork-cci. Must be called with ``self._lock`` held, right after a new
        worker is spawned and its read loop started, and before any prompt can be
        written. Writes the last ``initialize`` frame the gateway forwarded straight
        to worker stdin so ``_client_supports_question`` / plan-mode are set (and the
        AskUserQuestion tool unhidden) on the new worker exactly as they were on the
        original one. No-op when no initialize has been seen yet (the very first
        connection, where the backend's own initialize is still in flight and will
        arrive normally). Best-effort: a transport failure here must not wedge start().
        """
        frame = self._last_initialize_frame
        if frame is None:
            return
        try:
            await self._transport_write_stdin((frame + "\n").encode("utf-8"))
            logger.info(
                "Replayed initialize to fresh worker for session {sid} "
                "(capabilities re-declared)",
                sid=self.session_id,
            )
        except Exception as e:  # noqa: BLE001 — never wedge worker startup
            logger.warning(
                "Failed to replay initialize to worker for session {sid}: {err}",
                sid=self.session_id,
                err=f"{e.__class__.__name__}: {e}",
            )

    async def stop(self) -> None:
        """Stop the session: terminate worker and close all WebSockets."""
        await self.stop_worker(reason="stop")
        await self._close_all_websockets()

    async def stop_worker(
        self,
        *,
        reason: str | None = None,
        emit_status: bool = True,
    ) -> None:
        """Stop only the worker subprocess, keeping WebSockets connected."""
        async with self._lock:
            self._expecting_exit = True
            if self._process is not None:
                if self._process.returncode is None:
                    self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=10.0)
                except TimeoutError:
                    self._process.kill()
                    await self._process.wait()
                self._process = None

            if self._read_task is not None:
                self._read_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._read_task
                self._read_task = None

            self._in_flight_prompt_ids.clear()
            self._worker_id = None
            self._expecting_exit = False
            if emit_status:
                await self._emit_status("stopped", reason=reason or "stop")

    async def restart_worker(self, *, reason: str | None = None) -> None:
        """Restart the worker subprocess without disconnecting WebSockets."""
        started_at = time.perf_counter()
        await self._emit_status("restarting", reason=reason or "restart")
        await self.stop_worker(reason="restart", emit_status=False)
        await self.start(reason=reason or "restart", restart_started_at=started_at)

    async def _emit_restart_notice(self, *, reason: str | None, restart_ms: int) -> None:
        """Emit a restart notice to all WebSockets."""
        label = "Session restarted"
        if reason == "config_update":
            label = "Session restarted due to config update"
        payload = SessionNoticePayload(
            text=f"{label} · {restart_ms}ms",
            kind="restart",
            reason=reason,
            restart_ms=restart_ms,
        )
        event = SessionNoticeEvent(payload=payload)
        await self._broadcast(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": event.model_dump(mode="json"),
                },
                ensure_ascii=False,
            )
        )

    # ── byte-level transport primitives (overridable per backend) ────────────
    #
    # hechun-fork-cci: the JSON-RPC read loop + stdin framing below are backend
    # agnostic. The only backend-specific bits are these five primitives, which
    # the base class implements against ``self._process`` (asyncio subprocess —
    # also covers docker, whose ``docker run -i`` stdio IS the subprocess stdio).
    # ``CCISessionProcess`` (runner/cci_process.py) overrides them to drive a
    # ``KimoExecStream`` (channel.k8s.io exec WebSocket) instead, so docker and
    # CCI converge on one read loop / send path.

    async def _transport_read_stdout_line(self) -> bytes:
        """Read one newline-terminated JSON-RPC frame from the worker stdout."""
        assert self._process is not None
        assert self._process.stdout is not None
        return await self._process.stdout.readline()

    def _transport_stdout_at_eof(self) -> bool:
        """Whether the worker stdout has reached EOF (worker exited)."""
        assert self._process is not None
        assert self._process.stdout is not None
        return self._process.stdout.at_eof()

    async def _transport_read_stderr(self) -> bytes:
        """Drain the worker stderr (diagnostic on unexpected exit)."""
        assert self._process is not None
        assert self._process.stderr is not None
        return await self._process.stderr.read()

    def _transport_returncode(self) -> int | None:
        """Worker exit code, or None if still running / unknown."""
        return self._process.returncode if self._process is not None else None

    async def _transport_write_stdin(self, data: bytes) -> None:
        """Write a framed JSON-RPC message to the worker stdin and flush."""
        process = self._process
        assert process is not None
        assert process.stdin is not None
        process.stdin.write(data)
        await process.stdin.drain()

    async def _on_worker_exit(self, returncode: int | None, stderr: bytes) -> None:
        """Hook fired when the worker exits unexpectedly (before the generic
        error broadcast in :meth:`_read_loop`).

        No-op in the base class. Backends override it to react to specific exit
        codes — e.g. :class:`~kimi_cli.web.runner.cci_process.CCISessionProcess`
        maps ``AGENT_LOAD_FAILURE_EXIT_CODE`` onto the
        ``kimo_agent_load_failure_total`` metric and a clearer client message
        (hechun-fork-cci). Must never raise (it runs inside the read loop).
        """
        return None

    async def _read_loop(self) -> None:
        """Read messages from worker stdout and broadcast to WebSockets.

        Backend-agnostic: reads via :meth:`_transport_read_stdout_line` etc., so
        the subprocess/docker path and the CCI exec-WebSocket path share this
        exact loop (hechun-fork-cci).
        """
        try:
            while True:
                line = await self._transport_read_stdout_line()
                if not line:
                    if self._transport_stdout_at_eof():
                        if self._expecting_exit:
                            break
                        stderr = await self._transport_read_stderr()
                        if not stderr:
                            stderr = b"No stderr"
                        returncode = self._transport_returncode()
                        # Clear in-flight IDs before broadcasting so that
                        # is_busy is already False when the frontend reacts
                        # to the error and sends a new prompt.
                        self._in_flight_prompt_ids.clear()
                        # hechun-fork-cci: let backends react to specific worker
                        # exit codes (e.g. CCI maps AGENT_LOAD_FAILURE_EXIT_CODE
                        # → metric + clearer message). No-op in the base class, so
                        # docker/local behaviour is unchanged.
                        await self._on_worker_exit(returncode, stderr)
                        await self._broadcast(
                            JSONRPCErrorResponse(
                                id=str(uuid4()),
                                error=JSONRPCErrorObject(
                                    code=returncode or -1,
                                    message=stderr.decode("utf-8"),
                                ),
                            ).model_dump_json()
                        )
                        logger.warning(
                            f"Process exited with {returncode}: "
                            f"{stderr.decode('utf-8')}"
                        )
                        await self._emit_status(
                            "error",
                            reason="process_exit",
                            detail=stderr.decode("utf-8"),
                        )
                        break
                    else:
                        continue

                await self._broadcast(line.decode("utf-8").rstrip("\n"))

                # Handle out message
                try:
                    msg = json.loads(line)
                    match msg.get("method"):
                        case "event":
                            msg["params"] = deserialize_wire_message(msg["params"])
                            await self._handle_out_message(JSONRPCEventMessage.model_validate(msg))
                        case "request":
                            msg["params"] = deserialize_wire_message(msg["params"])
                            await self._handle_out_message(
                                JSONRPCRequestMessage.model_validate(msg)
                            )
                        case _:
                            if msg.get("error"):
                                await self._handle_out_message(
                                    JSONRPCErrorResponse.model_validate(msg)
                                )
                            else:
                                await self._handle_out_message(
                                    JSONRPCSuccessResponse.model_validate(msg)
                                )
                except json.JSONDecodeError:
                    logger.error(f"Invalid JSONRPC out message: {line}")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Unexpected error in read loop: {e.__class__.__name__} {e}")
            self._in_flight_prompt_ids.clear()
            # hechun-fork-cci: the read loop blew up (e.g. exec WebSocket dropped) —
            # this is also an unexpected worker death, so let backends react (CCI
            # deletes the now-worthless Pod here, otherwise the keepalive Pod leaks
            # Running forever — the very leak this branch was missing). No-op in the
            # base class, so docker/local behaviour is unchanged. Must not raise.
            with contextlib.suppress(Exception):
                await self._on_worker_exit(
                    self._transport_returncode(),
                    f"read loop error: {e.__class__.__name__} {e}".encode(),
                )
            await self._emit_status("error", reason="read_loop_error", detail=str(e))

    async def _handle_out_message(self, message: JSONRPCOutMessage) -> None:
        """Handle outbound message from worker."""
        match message:
            case JSONRPCSuccessResponse():
                was_busy = self.is_busy
                if message.id in self._in_flight_prompt_ids:
                    self._in_flight_prompt_ids.remove(message.id)
                if was_busy and not self.is_busy:
                    await self._emit_status("idle", reason="prompt_complete")
            case JSONRPCErrorResponse():
                was_busy = self.is_busy
                if message.id in self._in_flight_prompt_ids:
                    self._in_flight_prompt_ids.remove(message.id)
                if was_busy and not self.is_busy:
                    await self._emit_status("idle", reason="prompt_error")
            case _:
                return

    async def _encode_uploaded_files(self) -> AsyncGenerator[ContentPart]:
        """Encode uploaded files for sending to the model."""
        session = load_session_by_id(self.session_id)
        assert session is not None

        uploads_dir = session.kimi_cli_session.dir / "uploads"
        if not uploads_dir.exists():
            return

        # Load .sent marker left by fork to avoid re-sending inherited files.
        # The marker is kept (not deleted) so it survives process restarts.
        sent_marker = uploads_dir / ".sent"
        if sent_marker.exists():
            try:
                already_sent = json.loads(sent_marker.read_text(encoding="utf-8"))
                self._sent_files.update(already_sent)
            except Exception:
                pass

        all_files = sorted(
            (f for f in uploads_dir.iterdir() if f.name != ".sent"),
            key=lambda x: x.name,
        )
        files = [f for f in all_files if f.name not in self._sent_files]

        if not files:
            return

        # Build file list with paths and mime types
        file_infos: list[tuple[Path, str]] = []
        for file in files:
            mime_type, _ = mimetypes.guess_type(file.name)
            file_infos.append((file, mime_type or "application/octet-stream"))

        # Output file list summary
        file_list_lines = ["<uploaded_files>"]
        for idx, (file, _) in enumerate(file_infos, start=1):
            file_list_lines.append(f"{idx}. {file}")
        file_list_lines.append("</uploaded_files>")
        yield TextPart(text="\n".join(file_list_lines) + "\n\n")

        # Text file extensions
        text_extensions = {
            ".txt",
            ".md",
            ".json",
            ".yaml",
            ".yml",
            ".xml",
            ".html",
            ".css",
            ".js",
            ".ts",
            ".py",
            ".sh",
            ".csv",
            ".log",
            ".rst",
            ".toml",
            ".ini",
        }

        # Check model capabilities
        config = load_config()
        capabilities: set[ModelCapability] = set()
        if config.default_model and config.default_model in config.models:
            model_config = config.models[config.default_model]
            capabilities = derive_model_capabilities(model_config)
        else:
            # Fallback: derive from env var when config file has no model entry
            env_model_name = (
                os.environ.get("KIMI_MODEL_NAME")
                or os.environ.get("OPENAI_MODEL_NAME")
                or os.environ.get("ANTHROPIC_MODEL_NAME")
            )
            if env_model_name:
                capabilities = derive_model_capabilities(
                    LLMModel(provider="", model=env_model_name, max_context_size=100_000)
                )
        is_vision = "image_in" in capabilities
        is_video_in = "video_in" in capabilities

        # Process each file
        for file, mime_type in file_infos:
            file_path = str(file)
            ext = file.suffix.lower()

            if is_vision and mime_type.startswith("image/"):
                try:
                    content = file.read_bytes()
                    with Image.open(io.BytesIO(content)) as img:
                        pil_img: PILImage = img
                        width, height = pil_img.size
                        max_side = max(width, height)
                        if max_side > 4096:
                            scale = 4096 / max_side
                            new_size = (int(width * scale), int(height * scale))
                            pil_img = pil_img.resize(  # pyright: ignore[reportUnknownMemberType]
                                new_size
                            )
                        buffer = io.BytesIO()
                        pil_img.save(buffer, format="PNG")
                        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                        tag = f'<image path="{file_path}" content_type="{mime_type}">'
                        yield TextPart(text=tag)
                        yield ImageURLPart(
                            image_url=ImageURLPart.ImageURL(url=f"data:image/png;base64,{encoded}")
                        )
                        yield TextPart(text="</image>\n\n")
                except Exception:
                    logger.exception("Failed to encode uploaded image %s", file_path)
            elif is_video_in and mime_type.startswith("video/"):
                # For video files, emit a <video> tag for frontend display but don't embed content.
                # The agent will use ReadMediaFile tool to read it, which handles video uploads
                # properly.
                yield TextPart(text=f'<video path="{file_path}" content_type="{mime_type}">')
                yield TextPart(text="</video>\n\n")
            elif ext in text_extensions or mime_type.startswith("text/"):
                try:
                    content = file.read_bytes()
                    text_content = content.decode("utf-8", errors="replace")
                    yield TextPart(text=f'<document path="{file_path}" content_type="{mime_type}">')
                    yield TextPart(text=text_content)
                    yield TextPart(text="</document>\n\n")
                except Exception:
                    # Skip files that fail to decode - don't block the upload
                    pass

        # Mark files as sent and persist to disk so state survives server restarts.
        for file in files:
            self._sent_files.add(file.name)
        sent_marker = uploads_dir / ".sent"
        with contextlib.suppress(Exception):
            sent_marker.write_text(json.dumps(sorted(self._sent_files)), encoding="utf-8")

    async def _handle_in_message(self, message: JSONRPCInMessage) -> str | None:
        """Handle inbound message to worker, encoding uploaded files."""
        match message:
            case JSONRPCPromptMessage():
                user_input: list[ContentPart] = []
                async for part in self._encode_uploaded_files():
                    user_input.append(part)
                # Special marker for file-only uploads
                if isinstance(message.params.user_input, str):
                    if message.params.user_input != "KIMI_FILE_UPLOAD_WITHOUT_MESSAGE":
                        user_input.append(TextPart(text=message.params.user_input))
                else:
                    user_input += message.params.user_input
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "prompt",
                        "id": message.id,
                        "params": {
                            "user_input": [part.model_dump(mode="json") for part in user_input],
                        },
                    },
                    ensure_ascii=False,
                )
            case _:
                return None
        return None

    async def _broadcast(self, message: str) -> None:
        """Broadcast a message to all connected WebSockets."""
        disconnected: set[WebSocket] = set()

        async with self._ws_lock:
            websockets = list(self._websockets)
            to_send: list[WebSocket] = []
            for ws in websockets:
                buffer = self._replay_buffers.get(ws)
                if buffer is not None:
                    buffer.append(message)
                else:
                    to_send.append(ws)

        for ws in to_send:
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_text(message)
                else:
                    disconnected.add(ws)
            except Exception as e:
                logger.warning(f"websocket failed: {e.__class__.__name__} {e}")
                disconnected.add(ws)

        if disconnected:
            async with self._ws_lock:
                self._websockets -= disconnected
                self._websocket_count = len(self._websockets)
                for ws in disconnected:
                    self._replay_buffers.pop(ws, None)
            logger.debug(
                f"Broadcast: removed {len(disconnected)} disconnected ws, "
                f"remaining={self._websocket_count}"
            )

    async def add_websocket_and_begin_replay(self, ws: WebSocket) -> None:
        """Atomically attach a WebSocket and enter replay mode for it."""
        async with self._ws_lock:
            if ws not in self._websockets:
                self._websockets.add(ws)
                self._websocket_count = len(self._websockets)
            self._replay_buffers.setdefault(ws, [])
        logger.debug(f"WebSocket added (replay mode), count={self._websocket_count}")

    async def end_replay(self, ws: WebSocket) -> None:
        """Flush buffered live messages for a websocket after history replay."""
        while True:
            async with self._ws_lock:
                buffer = self._replay_buffers.get(ws)
                if buffer is None:
                    return
                if not buffer:
                    self._replay_buffers.pop(ws, None)
                    return
                chunk = buffer.copy()
                buffer.clear()

            if ws.client_state != WebSocketState.CONNECTED:
                logger.warning("end_replay: ws not connected, cleaning up replay buffer")
                async with self._ws_lock:
                    self._replay_buffers.pop(ws, None)
                return
            for message in chunk:
                try:
                    await ws.send_text(message)
                except Exception as e:
                    # Send failed — pop the replay buffer so _broadcast()
                    # sends directly (or detects disconnect) on the next call.
                    # Do NOT remove ws from _websockets here; let _broadcast()
                    # or session_stream's finally block handle cleanup.
                    logger.warning(f"end_replay: send_text failed during buffer flush: {e}")
                    async with self._ws_lock:
                        self._replay_buffers.pop(ws, None)
                    return

    async def _close_all_websockets(self) -> None:
        """Close all connected WebSockets."""
        async with self._ws_lock:
            websockets = list(self._websockets)
            self._websockets.clear()
            self._websocket_count = 0
            self._replay_buffers.clear()

        for ws in websockets:
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.close(code=1001, reason="Session process exited")
            except Exception:
                # Ignore errors closing already-disconnected WebSockets
                pass

    async def remove_websocket(self, ws: WebSocket) -> None:
        """Remove a WebSocket connection from this session."""
        async with self._ws_lock:
            if ws in self._websockets:
                self._websockets.discard(ws)
                self._websocket_count = len(self._websockets)
                logger.debug(f"WebSocket removed, count={self._websocket_count}")
            self._replay_buffers.pop(ws, None)

    async def send_message(self, message: str) -> None:
        """Send a message to the worker stdin (subprocess or CCI exec stdin)."""
        # hechun-fork-cci: any inbound message is activity — keep the idle sweeper
        # from reclaiming a session the user is actively driving.
        self._touch_active()
        # hechun-fork-cci: detect an ``initialize`` frame BEFORE start(). The
        # capability handshake must survive worker restarts, so we remember the
        # raw frame. Detection is a cheap top-level method check; we only parse the
        # full envelope for prompt/cancel handling below. We set
        # ``_last_initialize_frame`` AFTER start() so start()'s replay does not
        # double-send this very frame (start() spawns the worker, replay is a no-op
        # because the field is still None, then this frame flows through normally).
        is_initialize = _is_initialize_frame(message)

        await self.start()

        if is_initialize:
            self._last_initialize_frame = message

        # Handle in message
        try:
            in_message = JSONRPCInMessageAdapter.validate_json(message)
            if isinstance(in_message, JSONRPCPromptMessage):
                was_busy = self.is_busy
                self._in_flight_prompt_ids.add(in_message.id)
                if not was_busy:
                    await self._emit_status("busy", reason="prompt")
            elif isinstance(in_message, JSONRPCCancelMessage) and not self.is_busy:
                # If not busy, return success to avoid errors
                await self._broadcast(
                    JSONRPCSuccessResponse(id=in_message.id, result={}).model_dump_json()
                )
                return

            new_message = await self._handle_in_message(in_message)
            if new_message is not None:
                message = new_message
        except ValueError as e:
            logger.error(f"{e.__class__.__name__} {e}: Invalid JSONRPC in message: {message}")
            return

        await self._transport_write_stdin((message + "\n").encode("utf-8"))


class KimiCLIRunner:
    """Manages multiple session processes."""

    def __init__(self) -> None:
        """Initialize the runner."""
        self._sessions: dict[UUID, SessionProcess] = {}
        self._lock = asyncio.Lock()

    def start(self) -> None:
        """Start the runner (no-op, sessions started on demand)."""
        pass

    async def stop(self) -> None:
        """Stop all running sessions."""
        tasks: list[asyncio.Task[None]] = []
        for session in self._sessions.values():
            if session.is_running:
                tasks.append(asyncio.create_task(session.stop()))
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5.0)
            for t in pending:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t

    async def get_or_create_session(self, session_id: UUID) -> SessionProcess:
        """Get or create a session process."""
        async with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = SessionProcess(session_id)
            return self._sessions[session_id]

    def get_session(self, session_id: UUID) -> SessionProcess | None:
        """Get a session process if it exists."""
        return self._sessions.get(session_id)

    def iter_sessions(self) -> list[tuple[UUID, SessionProcess]]:
        """Snapshot of (session_id, process) pairs currently tracked.

        hechun-fork-cci: enumeration entry point for the CCI idle sweeper
        (web/app.py). Returns a list copy so callers can iterate without holding
        the runner lock or racing concurrent get_or_create_session mutations.
        """
        return list(self._sessions.items())

    async def detach_websocket(self, ws: WebSocket, session_id: UUID) -> None:
        """Detach a WebSocket from a session."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if session:
                await session.remove_websocket(ws)

    async def restart_running_workers(
        self,
        *,
        reason: str,
        force: bool,
    ) -> RestartWorkersSummary:
        """Restart all running workers to apply global config updates.

        Args:
            reason: Reason for the restart (e.g., "config_update")
            force: If True, also restart busy sessions (may interrupt prompts)

        Returns:
            Summary of restarted and skipped sessions
        """
        async with self._lock:
            running = [(sid, proc) for sid, proc in self._sessions.items() if proc.is_running]

        restarted: list[UUID] = []
        skipped_busy: list[UUID] = []
        tasks: list[asyncio.Task[None]] = []

        for session_id, proc in running:
            if proc.is_busy and not force:
                skipped_busy.append(session_id)
                continue
            restarted.append(session_id)
            tasks.append(asyncio.create_task(proc.restart_worker(reason=reason)))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        return RestartWorkersSummary(
            restarted_session_ids=restarted,
            skipped_busy_session_ids=skipped_busy,
        )


@dataclass(slots=True)
class RestartWorkersSummary:
    """Summary of a restart_running_workers operation."""

    restarted_session_ids: list[UUID]
    skipped_busy_session_ids: list[UUID]
