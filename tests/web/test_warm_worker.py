"""Warm-pool worker side: two-phase startup + handshake (W2).

# hechun-fork-cci (warm pool)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from kimi_cli.web.runner import warm_protocol as wp
from kimi_cli.web.runner import worker as W


class TestUnbufferedStdinRead:
    def test_does_not_read_past_the_newline(self):
        """The handshake reader must leave the rest of the pipe untouched.

        After bind, ``WireServer`` opens its OWN reader on fd 0. The gateway
        writes ``initialize`` immediately after the bind frame, so a buffered
        reader would routinely pull it into a buffer nobody ever drains and the
        session would lose its capability handshake without a trace.
        """
        r, w = os.pipe()
        os.write(w, b'{"kimo_warm":"bind"}\n{"jsonrpc":"2.0","method":"initialize"}\n')
        os.close(w)
        try:
            first = W._read_stdin_line_unbuffered(r)
            assert first == b'{"kimo_warm":"bind"}'
            # The initialize frame is still in the pipe, byte for byte.
            rest = b""
            while True:
                chunk = os.read(r, 4096)
                if not chunk:
                    break
                rest += chunk
            assert rest == b'{"jsonrpc":"2.0","method":"initialize"}\n'
        finally:
            os.close(r)

    def test_returns_none_at_eof(self):
        r, w = os.pipe()
        os.close(w)
        try:
            assert W._read_stdin_line_unbuffered(r) is None
        finally:
            os.close(r)


class TestVerifyStaticAssets:
    def test_ok_when_the_agent_spec_landed(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SUBAGENT", "diabetes-expert")
        monkeypatch.setattr(
            W, "resolve_subagent_yaml", lambda *a, **k: Path("/root/.kimi/agents/x.yaml")
        )
        result = W._verify_static_assets()
        assert result.ok is True
        assert result.agent_path == "/root/.kimi/agents/x.yaml"

    def test_not_ok_when_the_download_silently_failed(self, monkeypatch: pytest.MonkeyPatch):
        """``_fetch_sandbox_assets`` never raises — so "the process started" is
        not evidence the Pod can serve anything. Without this check the Pod would
        be advertised ready and fail every claim, forever."""
        monkeypatch.setenv("SUBAGENT", "diabetes-expert")
        monkeypatch.setattr(W, "resolve_subagent_yaml", lambda *a, **k: None)
        result = W._verify_static_assets()
        assert result.ok is False
        assert result.reason == "assets_missing"


class TestBindAndRun:
    async def test_identity_reaches_os_environ_before_anything_is_built(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The bind identity must be in ``os.environ`` before MCP config load.

        The MCP header ``X-Hechun-User: ${KIMI_USER_ID}`` is substituted from
        ``os.environ`` when the client is constructed, and that connection is
        never rebuilt — bind the identity late and the tools either fail or, far
        worse, carry the wrong user.
        """
        seen: dict[str, str] = {}

        class _Stop(Exception):
            pass

        def _spy(_sid):
            seen.update(os.environ)
            raise _Stop

        monkeypatch.setattr(W, "load_session_by_id", _spy)
        monkeypatch.delenv("KIMI_USER_ID", raising=False)
        monkeypatch.delenv("KIMO_DEFAULT_YOLO", raising=False)

        sid = uuid4()
        with pytest.raises(_Stop):
            await W.bind_and_run(sid, owner_id="hechun-77", yolo=True)

        assert seen["KIMI_USER_ID"] == "hechun-77"
        assert seen["KIMO_DEFAULT_YOLO"] == "1"
        assert seen["KIMI_SESSION_ID"] == str(sid)

    async def test_yolo_false_is_bound_as_false_not_dropped(self, monkeypatch: pytest.MonkeyPatch):
        seen: dict[str, str] = {}

        class _Stop(Exception):
            pass

        def _spy(_sid):
            seen.update(os.environ)
            raise _Stop

        monkeypatch.setattr(W, "load_session_by_id", _spy)
        monkeypatch.setenv("KIMO_DEFAULT_YOLO", "1")  # stale Pod-level default
        with pytest.raises(_Stop):
            await W.bind_and_run(uuid4(), owner_id="u", yolo=False)
        assert seen["KIMO_DEFAULT_YOLO"] == "0"

    async def test_cold_path_leaves_env_alone(self, monkeypatch: pytest.MonkeyPatch):
        """No bind values ⇒ the pre-existing env/storage logic is untouched."""
        monkeypatch.delenv("KIMI_USER_ID", raising=False)

        class _Stop(Exception):
            pass

        def _spy(_sid):
            raise _Stop

        monkeypatch.setattr(W, "load_session_by_id", _spy)
        with pytest.raises(_Stop):
            await W.bind_and_run(uuid4())
        assert "KIMI_USER_ID" not in os.environ


class _StdinFeeder:
    """Feeds scripted lines to ``_read_stdin_line_unbuffered``."""

    def __init__(self, lines):
        self._lines = [line.rstrip("\n").encode("utf-8") for line in lines]

    def __call__(self, fd=0):
        return self._lines.pop(0) if self._lines else None


class TestWarmHandshake:
    async def test_ping_is_answered_then_bind_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        sid = uuid4()
        monkeypatch.setattr(
            W,
            "_read_stdin_line_unbuffered",
            _StdinFeeder(
                [
                    wp.encode(wp.FRAME_PING, seq="abc"),
                    wp.encode(wp.FRAME_BIND, session_id=str(sid), owner_id="hechun-9", yolo=True),
                ]
            ),
        )
        bind = await W._warm_handshake(0.0)
        assert bind.session_id == sid
        assert bind.owner_id == "hechun-9"
        assert bind.yolo is True

        pong = wp.decode(capsys.readouterr().out.splitlines()[0])
        assert pong[wp.WARM_KEY] == wp.FRAME_PONG
        assert pong["seq"] == "abc"

    @pytest.mark.parametrize(
        "payload",
        [
            {"yolo": True},  # no owner_id
            {"owner_id": "u"},  # no yolo
        ],
    )
    async def test_bind_missing_owner_or_yolo_fails_the_claim(
        self, monkeypatch: pytest.MonkeyPatch, capsys, payload
    ):
        """Missing owner/yolo ⇒ refuse, exit, let the gateway cold-start.

        Accepting the bind anyway would run the session with no owner and
        ``yolo=False``, which strands every tool call on an approval prompt the
        mobile clients cannot answer (spec §4.3).
        """
        payload = {"session_id": str(uuid4()), **payload}
        monkeypatch.setattr(
            W,
            "_read_stdin_line_unbuffered",
            _StdinFeeder([wp.encode(wp.FRAME_BIND, **payload)]),
        )
        with pytest.raises(SystemExit) as exc:
            await W._warm_handshake(0.0)
        assert exc.value.code == wp.WARM_BIND_FAILURE_EXIT_CODE
        err = wp.decode(capsys.readouterr().out.splitlines()[0])
        assert err[wp.WARM_KEY] == wp.FRAME_ERROR
        assert err["reason"] == wp.REASON_BIND_INVALID

    async def test_stray_jsonrpc_before_bind_is_not_mistaken_for_one(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        sid = uuid4()
        monkeypatch.setattr(
            W,
            "_read_stdin_line_unbuffered",
            _StdinFeeder(
                [
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
                    wp.encode(wp.FRAME_BIND, session_id=str(sid), owner_id="u", yolo=False),
                ]
            ),
        )
        bind = await W._warm_handshake(0.0)
        assert bind.session_id == sid

    async def test_eof_before_bind_exits_cleanly(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(W, "_read_stdin_line_unbuffered", _StdinFeeder([]))
        with pytest.raises(SystemExit) as exc:
            await W._warm_handshake(0.0)
        assert exc.value.code == 0


class TestRunWarmWorker:
    async def test_failed_preparation_reports_and_exits(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        async def _prep():
            return W.PrepareResult(ok=False, reason="assets_missing", agent_name="diabetes-expert")

        monkeypatch.setattr(W, "prepare_static", _prep)
        with pytest.raises(SystemExit) as exc:
            await W.run_warm_worker()
        assert exc.value.code == wp.WARM_PREPARE_FAILURE_EXIT_CODE
        ready = wp.decode(capsys.readouterr().out.splitlines()[0])
        assert ready[wp.WARM_KEY] == wp.FRAME_READY
        assert ready["ok"] is False
        assert ready["reason"] == "assets_missing"

    async def test_happy_path_reports_ready_then_binds(
        self, monkeypatch: pytest.MonkeyPatch, capsys
    ):
        sid = uuid4()
        ran: dict = {}

        async def _prep():
            return W.PrepareResult(ok=True, agent_name="diabetes-expert", prepare_ms=42.0)

        async def _bind_and_run(session_id, *, owner_id=None, yolo=None):
            ran.update(sid=session_id, owner=owner_id, yolo=yolo, env=dict(os.environ))

        monkeypatch.setattr(W, "prepare_static", _prep)
        monkeypatch.setattr(W, "bind_and_run", _bind_and_run)
        monkeypatch.setattr(
            W,
            "_read_stdin_line_unbuffered",
            _StdinFeeder(
                [
                    wp.encode(
                        wp.FRAME_BIND,
                        session_id=str(sid),
                        owner_id="hechun-3",
                        yolo=True,
                        env={"SUBAGENT": "diabetes-expert", "HTTPS_PROXY": "http://evil"},
                    )
                ]
            ),
        )

        await W.run_warm_worker()

        out = [wp.decode(line) for line in capsys.readouterr().out.splitlines()]
        assert out[0][wp.WARM_KEY] == wp.FRAME_READY
        assert out[0]["ok"] is True
        assert out[1][wp.WARM_KEY] == wp.FRAME_BOUND
        assert ran["sid"] == sid
        assert ran["owner"] == "hechun-3"
        assert ran["yolo"] is True
        # allowlisted env applied; everything else dropped
        assert ran["env"]["SUBAGENT"] == "diabetes-expert"
        assert ran["env"].get("HTTPS_PROXY") != "http://evil"
