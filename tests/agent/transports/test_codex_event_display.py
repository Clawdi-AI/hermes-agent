"""Tests for the codex_app_server display bridge.

Drives ``build_event_display_callback`` against captured ``item/*``
notifications and asserts that it calls into the same agent tool
progress / start / complete callbacks the chat_completions loop uses
in ``agent/tool_executor.py``.

This is the visibility patch behind GH issue: codex_app_server turns
look like a black box because no on_event hook was wired in. The bridge
fixes that without touching the chat_completions path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.transports.codex_event_display import build_event_display_callback


# ---------- helpers ----------

def _agent_with_recorders() -> SimpleNamespace:
    """Build a stub agent that records every tool_*_callback call."""
    progress: list[tuple] = []
    starts: list[tuple] = []
    completes: list[tuple] = []

    agent = SimpleNamespace(
        tool_progress_callback=lambda event_type, name=None, preview=None, args=None, **kwargs: progress.append(
            (event_type, name, preview, args, kwargs)
        ),
        tool_start_callback=lambda item_id, name, args: starts.append((item_id, name, args)),
        tool_complete_callback=lambda item_id, name, args, result: completes.append((item_id, name, args, result)),
    )
    agent._progress = progress
    agent._starts = starts
    agent._completes = completes
    return agent


def _started(item_type: str, item_id: str, **fields) -> dict:
    return {
        "method": "item/started",
        "params": {"item": {"type": item_type, "id": item_id, **fields}},
    }


def _completed(item_type: str, item_id: str, **fields) -> dict:
    return {
        "method": "item/completed",
        "params": {"item": {"type": item_type, "id": item_id, **fields}},
    }


# ---------- non-tool items ----------

class TestNonToolItemsAreIgnored:
    """agentMessage / reasoning / userMessage / opaque items are ignored —
    they materialise via the projector into the messages list, not as
    tool-progress callbacks."""

    @pytest.mark.parametrize(
        "item_type", ["agentMessage", "reasoning", "userMessage", "plan", "hookPrompt"],
    )
    def test_no_callbacks_fired(self, item_type):
        agent = _agent_with_recorders()
        cb = build_event_display_callback(agent)
        cb(_started(item_type, "x", text="..."))
        cb(_completed(item_type, "x", text="..."))
        assert agent._progress == []
        assert agent._starts == []
        assert agent._completes == []


# ---------- commandExecution ----------

class TestCommandExecution:
    def test_started_fires_tool_started_with_command_preview(self):
        agent = _agent_with_recorders()
        cb = build_event_display_callback(agent)
        cb(_started(
            "commandExecution", "c1",
            command="ls -la /tmp", cwd="/tmp",
        ))
        assert len(agent._progress) == 1
        event_type, name, preview, args, kwargs = agent._progress[0]
        assert event_type == "tool.started"
        assert name == "exec_command"
        assert preview == "ls -la /tmp"
        assert args == {"command": "ls -la /tmp", "cwd": "/tmp"}
        # tool_start_callback also fires with the item id
        assert agent._starts == [("c1", "exec_command", {"command": "ls -la /tmp", "cwd": "/tmp"})]

    def test_completed_fires_tool_completed_with_duration_and_no_error(self):
        agent = _agent_with_recorders()
        cb = build_event_display_callback(agent)
        cb(_started("commandExecution", "c2", command="echo hi", cwd="/"))
        cb(_completed(
            "commandExecution", "c2",
            command="echo hi", cwd="/",
            exitCode=0, aggregatedOutput="hi\n",
        ))
        # First entry is the start, second is the completion
        assert agent._progress[1][0] == "tool.completed"
        assert agent._progress[1][1] == "exec_command"
        kwargs = agent._progress[1][4]
        assert kwargs["is_error"] is False
        assert kwargs["duration"] >= 0.0
        # complete callback fires with item id + a short result digest
        assert len(agent._completes) == 1
        item_id, name, args, result = agent._completes[0]
        assert item_id == "c2"
        assert name == "exec_command"
        assert "exit=0" in result

    def test_nonzero_exit_marks_error(self):
        agent = _agent_with_recorders()
        cb = build_event_display_callback(agent)
        cb(_started("commandExecution", "c3", command="false", cwd="/"))
        cb(_completed("commandExecution", "c3", command="false", cwd="/", exitCode=1))
        kwargs = agent._progress[1][4]
        assert kwargs["is_error"] is True


# ---------- fileChange ----------

class TestFileChange:
    def test_started_preview_summarises_kinds_and_paths(self):
        agent = _agent_with_recorders()
        cb = build_event_display_callback(agent)
        cb(_started(
            "fileChange", "fc1",
            changes=[
                {"kind": {"type": "add"}, "path": "/tmp/a.py"},
                {"kind": {"type": "update"}, "path": "/tmp/b.py"},
            ],
        ))
        event_type, name, preview, args, _ = agent._progress[0]
        assert event_type == "tool.started"
        assert name == "apply_patch"
        assert "1 add" in preview and "1 update" in preview
        assert "/tmp/a.py" in preview
        # args mirrors what the projector uses for the tool_call
        assert args == {"changes": [
            {"kind": "add", "path": "/tmp/a.py"},
            {"kind": "update", "path": "/tmp/b.py"},
        ]}

    def test_failed_status_marks_error(self):
        agent = _agent_with_recorders()
        cb = build_event_display_callback(agent)
        cb(_started("fileChange", "fc2", changes=[]))
        cb(_completed("fileChange", "fc2", changes=[], status="failed"))
        kwargs = agent._progress[1][4]
        assert kwargs["is_error"] is True


# ---------- mcpToolCall ----------

class TestMcpToolCall:
    def test_name_includes_server_and_tool(self):
        agent = _agent_with_recorders()
        cb = build_event_display_callback(agent)
        cb(_started(
            "mcpToolCall", "mc1",
            server="linear", tool="search_issues", arguments={"query": "bugs"},
        ))
        _event, name, preview, args, _ = agent._progress[0]
        assert name == "mcp.linear.search_issues"
        assert preview and "bugs" in preview
        assert args == {"query": "bugs"}

    def test_error_marks_completed_as_error(self):
        agent = _agent_with_recorders()
        cb = build_event_display_callback(agent)
        cb(_started("mcpToolCall", "mc2", server="s", tool="t"))
        cb(_completed("mcpToolCall", "mc2", server="s", tool="t",
                       error={"code": -1, "message": "oops"}))
        kwargs = agent._progress[1][4]
        assert kwargs["is_error"] is True
        result = agent._completes[0][3]
        assert result == "error"


# ---------- dynamicToolCall ----------

class TestDynamicToolCall:
    def test_uses_tool_name_directly(self):
        agent = _agent_with_recorders()
        cb = build_event_display_callback(agent)
        cb(_started(
            "dynamicToolCall", "dt1",
            tool="web_search", arguments={"query": "Phala TDX"},
        ))
        _event, name, preview, args, _ = agent._progress[0]
        assert name == "web_search"
        assert "Phala TDX" in preview
        assert args == {"query": "Phala TDX"}

    def test_success_false_marks_error(self):
        agent = _agent_with_recorders()
        cb = build_event_display_callback(agent)
        cb(_started("dynamicToolCall", "dt2", tool="t"))
        cb(_completed("dynamicToolCall", "dt2", tool="t", success=False))
        kwargs = agent._progress[1][4]
        assert kwargs["is_error"] is True


# ---------- robustness ----------

class TestRobustness:
    def test_missing_callback_is_no_op(self):
        # Agent with no callbacks at all — display bridge must not raise.
        agent = SimpleNamespace()
        cb = build_event_display_callback(agent)
        cb(_started("commandExecution", "x", command="ls", cwd="/"))
        cb(_completed("commandExecution", "x", command="ls", cwd="/", exitCode=0))
        # Nothing to assert — just that it didn't blow up.

    def test_callback_exception_is_swallowed(self):
        # A throwing tool_progress_callback must not propagate up to the
        # codex polling loop.
        def boom(*a, **kw):
            raise RuntimeError("bang")
        agent = SimpleNamespace(tool_progress_callback=boom)
        cb = build_event_display_callback(agent)
        cb(_started("commandExecution", "x", command="ls", cwd="/"))
        cb(_completed("commandExecution", "x", command="ls", cwd="/", exitCode=0))

    def test_unknown_method_is_ignored(self):
        agent = _agent_with_recorders()
        cb = build_event_display_callback(agent)
        cb({"method": "turn/started", "params": {}})
        cb({"method": "item/commandExecution/outputDelta", "params": {"delta": "x"}})
        assert agent._progress == []

    def test_orphan_completed_without_started_still_fires(self):
        # If we somehow miss the item/started (e.g. event before bridge
        # was installed), item/completed should still fire — duration is
        # 0.0 in that case.
        agent = _agent_with_recorders()
        cb = build_event_display_callback(agent)
        cb(_completed("commandExecution", "orphan", command="ls", cwd="/", exitCode=0))
        assert len(agent._progress) == 1
        assert agent._progress[0][0] == "tool.completed"
        assert agent._progress[0][4]["duration"] == 0.0
