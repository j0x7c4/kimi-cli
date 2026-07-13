"""Memory ``list`` loop circuit breaker (hechun-fork).

Some models spin forever calling ``Memory op=list`` on empty persistent memory
(SIT: StepBegin 29→30→… all list, all empty, never ``add``). The breaker turns
the 2nd+ consecutive identical-scope list into a forcing instruction so the model
must break out (``add`` or answer). A single occasional list, or a list after any
add/update/delete, is judged fresh and lists normally.
"""

from __future__ import annotations

import pytest

from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.memory import (
    AddOp,
    DeleteOp,
    ListOp,
    Memory,
    Params,
)


@pytest.fixture
def memory_tool(runtime: Runtime) -> Memory:
    return Memory(runtime)


def _is_breaker(result) -> bool:
    return "STOP" in result.output and "Do NOT call Memory list again" in result.output


class TestListLoopBreaker:
    async def test_single_list_is_normal(self, memory_tool: Memory):
        """One list returns the real (empty) listing, never the breaker."""
        result = await memory_tool(Params(operation=ListOp(scope="persistent")))
        assert not result.is_error
        assert not _is_breaker(result)
        assert "Persistent memory: (empty)" in result.output

    async def test_second_consecutive_list_trips_breaker(self, memory_tool: Memory):
        """2nd identical-scope list in a row → forcing instruction, not a listing."""
        first = await memory_tool(Params(operation=ListOp(scope="persistent")))
        assert not _is_breaker(first)

        second = await memory_tool(Params(operation=ListOp(scope="persistent")))
        assert not second.is_error
        assert _is_breaker(second)
        # The forcing text tells the model to add or answer, and names the scope.
        assert "persistent" in second.output
        assert "op=add" in second.output

    async def test_third_list_still_broken(self, memory_tool: Memory):
        for _ in range(2):
            await memory_tool(Params(operation=ListOp(scope="persistent")))
        third = await memory_tool(Params(operation=ListOp(scope="persistent")))
        assert _is_breaker(third)

    async def test_scope_change_is_judged_fresh(self, memory_tool: Memory):
        """A list of a DIFFERENT scope is not the same loop → lists normally."""
        await memory_tool(Params(operation=ListOp(scope="persistent")))
        other = await memory_tool(Params(operation=ListOp(scope="session")))
        assert not _is_breaker(other)
        # …but a second identical (session) list now trips.
        again = await memory_tool(Params(operation=ListOp(scope="session")))
        assert _is_breaker(again)

    async def test_add_resets_breaker(
        self, memory_tool: Memory, monkeypatch: pytest.MonkeyPatch
    ):
        """After an ``add`` (real progress), a following list lists normally again."""

        async def _noop_approval(*_a, **_kw):
            return None

        monkeypatch.setattr(memory_tool, "_request_persistent_approval", _noop_approval)
        # Keep the add on the file/session path — irrelevant to the breaker.
        monkeypatch.setenv("KIMI_USER_ID", "hechun-1")

        await memory_tool(Params(operation=ListOp(scope="persistent")))
        # add uses session scope so it stays local (no storage/wire needed).
        add_res = await memory_tool(
            Params(operation=AddOp(kind="user", scope="session", content="name is Jay"))
        )
        assert not add_res.is_error
        # The list after the add is fresh again (not tripped).
        after = await memory_tool(Params(operation=ListOp(scope="persistent")))
        assert not _is_breaker(after)

    async def test_add_itself_never_breaks(
        self, memory_tool: Memory, monkeypatch: pytest.MonkeyPatch
    ):
        """The breaker only targets list; add is unaffected regardless of history."""
        # Two lists first (arm + trip), then add must still succeed normally.
        await memory_tool(Params(operation=ListOp(scope="persistent")))
        await memory_tool(Params(operation=ListOp(scope="persistent")))
        add_res = await memory_tool(
            Params(operation=AddOp(kind="user", scope="session", content="x"))
        )
        assert not add_res.is_error
        assert "STOP" not in add_res.output

    async def test_delete_resets_breaker(self, memory_tool: Memory):
        """A non-list op (delete) between lists resets the consecutive counter."""
        from tests.conftest import tool_call_context

        await memory_tool(Params(operation=ListOp(scope="persistent")))
        # delete of a missing id — the op runs (needs a tool-call context for its
        # persistent-approval path) and resets breaker state regardless of result.
        with tool_call_context("Memory"):
            await memory_tool(Params(operation=DeleteOp(id="does-not-exist")))
        after = await memory_tool(Params(operation=ListOp(scope="persistent")))
        assert not _is_breaker(after)
