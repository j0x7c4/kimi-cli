"""Tests for wire protocol plan mode support."""

from __future__ import annotations

from unittest.mock import MagicMock

from kimi_cli.soul.toolset import KimiToolset
from kimi_cli.tools.plan import ExitPlanMode
from kimi_cli.tools.plan.enter import EnterPlanMode
from kimi_cli.wire.jsonrpc import ClientCapabilities


class TestClientCapabilities:
    def test_defaults_to_false(self) -> None:
        caps = ClientCapabilities()
        assert caps.supports_plan_mode is False

    def test_parses_true(self) -> None:
        caps = ClientCapabilities(supports_plan_mode=True)
        assert caps.supports_plan_mode is True


class TestSyncPlanModeToolVisibility:
    def _make_toolset_with_plan_tools(self) -> KimiToolset:
        ts = KimiToolset()
        ts.add(ExitPlanMode())
        ts.add(EnterPlanMode())
        return ts

    def _make_server(self, supports_plan_mode: bool):
        """Create a minimal WireServer-like object with _sync_plan_mode_tool_visibility."""
        from kimi_cli.wire.server import WireServer

        # We need to construct WireServer with minimal mocking
        soul = MagicMock()
        soul.agent = MagicMock()
        soul.agent.runtime = MagicMock()
        soul.agent.runtime.labor_market.builtin_types = {}

        server = WireServer.__new__(WireServer)
        server._soul = soul
        server._client_supports_plan_mode = supports_plan_mode
        return server

    def test_hides_tools_when_unsupported(self) -> None:
        ts = self._make_toolset_with_plan_tools()
        server = self._make_server(supports_plan_mode=False)

        server._sync_plan_mode_tool_visibility(ts)

        # Tools should be hidden
        tool_names = {t.name for t in ts.tools}
        assert "ExitPlanMode" not in tool_names
        assert "EnterPlanMode" not in tool_names

    def test_tools_visible_when_supported(self) -> None:
        ts = self._make_toolset_with_plan_tools()
        server = self._make_server(supports_plan_mode=True)

        server._sync_plan_mode_tool_visibility(ts)

        tool_names = {t.name for t in ts.tools}
        assert "ExitPlanMode" in tool_names
        assert "EnterPlanMode" in tool_names

    def test_unhide_after_hide(self) -> None:
        ts = self._make_toolset_with_plan_tools()
        server = self._make_server(supports_plan_mode=False)

        # First hide
        server._sync_plan_mode_tool_visibility(ts)
        assert "ExitPlanMode" not in {t.name for t in ts.tools}

        # Then unhide
        server._client_supports_plan_mode = True
        server._sync_plan_mode_tool_visibility(ts)
        assert "ExitPlanMode" in {t.name for t in ts.tools}
        assert "EnterPlanMode" in {t.name for t in ts.tools}


class TestSyncAskUserToolVisibility:
    """hechun fork: AskUserQuestion visibility must track the client's
    supports_question capability — the handshake the CCI gateway now replays
    across worker restarts."""

    def _make_toolset_with_ask_user(self) -> KimiToolset:
        from kimi_cli.tools.ask_user import AskUserQuestion

        ts = KimiToolset()
        ts.add(AskUserQuestion())
        return ts

    def _make_server(self, supports_question: bool):
        from kimi_cli.wire.server import WireServer

        server = WireServer.__new__(WireServer)
        server._soul = MagicMock()
        server._client_supports_question = supports_question
        return server

    def test_ask_user_hidden_when_unsupported(self) -> None:
        from kimi_cli.tools.ask_user import NAME as ASK_USER

        ts = self._make_toolset_with_ask_user()
        server = self._make_server(supports_question=False)
        server._sync_ask_user_tool_visibility(ts)
        assert ASK_USER not in {t.name for t in ts.tools}

    def test_ask_user_visible_when_supported(self) -> None:
        from kimi_cli.tools.ask_user import NAME as ASK_USER

        ts = self._make_toolset_with_ask_user()
        server = self._make_server(supports_question=True)
        server._sync_ask_user_tool_visibility(ts)
        assert ASK_USER in {t.name for t in ts.tools}

    def test_unhide_after_hide(self) -> None:
        from kimi_cli.tools.ask_user import NAME as ASK_USER

        ts = self._make_toolset_with_ask_user()
        server = self._make_server(supports_question=False)
        server._sync_ask_user_tool_visibility(ts)
        assert ASK_USER not in {t.name for t in ts.tools}

        # Simulate a replayed initialize declaring supports_question=True.
        server._client_supports_question = True
        server._sync_ask_user_tool_visibility(ts)
        assert ASK_USER in {t.name for t in ts.tools}
