"""Tests for ``AIAgent.interrupt()`` propagating to an active codex_app_server
session.

Without this propagation, ``/stop`` on the codex_app_server runtime is
effectively a no-op until the post-tool quiet watchdog (90 s) or the
outer deadline (600 s) fires. With it, the session sees the interrupt
on its next polling tick and issues ``turn/interrupt`` to codex.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolate_hermes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir(exist_ok=True)


def _make_agent_with_session(monkeypatch, *, with_session: bool = True):
    """Build a stub AIAgent with the real ``interrupt`` method bound, and
    an optional codex_session MagicMock."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "")
    import run_agent as _ra

    class _Stub:
        _interrupt_requested = False
        _interrupt_message = None
        _execution_thread_id = threading.current_thread().ident
        _interrupt_thread_signal_pending = False
        log_prefix = ""
        quiet_mode = True
        verbose_logging = False
        log_prefix_chars = 200
        _print_fn = print
        _active_children: list = []

        def __init__(self):
            self._tool_worker_threads: set = set()
            self._tool_worker_threads_lock = threading.Lock()
            self._active_children_lock = threading.Lock()

    stub = _Stub()
    stub.interrupt = _ra.AIAgent.interrupt.__get__(stub)
    if with_session:
        stub._codex_session = MagicMock()
    return stub


def test_interrupt_propagates_to_codex_session(monkeypatch):
    """The active codex_app_server session must see request_interrupt()
    when AIAgent.interrupt() fires. Otherwise the codex subprocess keeps
    chewing on the interrupted turn until a server-side timeout."""
    agent = _make_agent_with_session(monkeypatch)
    agent.interrupt(message="please stop")
    assert agent._interrupt_requested is True
    agent._codex_session.request_interrupt.assert_called_once_with()


def test_interrupt_is_noop_when_no_codex_session(monkeypatch):
    """When the agent has never spawned a codex session (default runtime
    path, or pre-first-turn), interrupt() must not crash on a missing
    attribute."""
    agent = _make_agent_with_session(monkeypatch, with_session=False)
    # Sanity: the attribute really is absent (mirrors the production lazy
    # session creation that only assigns _codex_session on first turn).
    assert not hasattr(agent, "_codex_session")
    agent.interrupt(message="please stop")  # must not raise
    assert agent._interrupt_requested is True


def test_interrupt_swallows_codex_session_exception(monkeypatch):
    """If request_interrupt() raises (e.g. session in a weird state),
    the surrounding interrupt() flow must still run to completion so
    other interrupt fan-outs (worker threads, child agents) still fire."""
    agent = _make_agent_with_session(monkeypatch)
    agent._codex_session.request_interrupt.side_effect = RuntimeError("bang")
    agent.interrupt(message="please stop")
    # Despite the raise, the request_interrupt was attempted exactly once
    # and the agent's interrupt-requested flag is still set.
    assert agent._codex_session.request_interrupt.call_count == 1
    assert agent._interrupt_requested is True


def test_interrupt_after_session_closed_uses_session_attr_as_is(monkeypatch):
    """When _codex_session is set to None (the runtime sets this after a
    retired session), interrupt() must skip the propagation cleanly."""
    agent = _make_agent_with_session(monkeypatch)
    agent._codex_session = None
    agent.interrupt(message="please stop")
    assert agent._interrupt_requested is True
