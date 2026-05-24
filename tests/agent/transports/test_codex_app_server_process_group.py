"""Process-group cleanup test for ``CodexAppServerClient``.

The shipped ``/usr/local/bin/codex`` is a node wrapper that re-execs the
native ``codex`` binary as a child. Without ``start_new_session=True``,
``Popen.terminate()`` only signals the wrapper — the native child becomes
an orphan that keeps the rollout sqlite locked. This test exercises the
same shape: a parent shell that spawns a long-running grandchild, then
asserts ``close()`` reaps both.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from agent.transports.codex_app_server import CodexAppServerClient


# We import the class but spawn a fake "codex" binary — a shell that
# wraps a sleep grandchild, the same way /usr/local/bin/codex wraps the
# native binary.  No actual codex needed.
_WRAPPER_SCRIPT = r"""
# Spawn a long-running grandchild and keep it alive past our own exit.
exec /bin/sh -c '
  ( /bin/sleep 60 ) &
  echo $! > "$1"
  # Wrapper waits on stdin so it stays alive until killed.
  while read line; do :; done
' -- "$1"
"""


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="killpg / setsid are POSIX-only; the orphan bug only manifests on POSIX",
)
def test_close_reaps_native_grandchild(tmp_path, monkeypatch):
    """close() must terminate the whole process group, not just the
    immediate child, so the native ``codex`` binary doesn't survive as
    an orphan grandchild."""
    pidfile = tmp_path / "grandchild.pid"
    wrapper = tmp_path / "fake-codex"
    wrapper.write_text(_WRAPPER_SCRIPT)

    # Monkey-patch Popen *globally* so CodexAppServerClient spawns our
    # fake wrapper instead of looking for `codex` on PATH. We hand-craft
    # the argv to bypass the codex_bin lookup.
    real_popen = subprocess.Popen

    def _fake_popen(cmd, **kwargs):
        # Replace `codex app-server <extra>` with our wrapper invocation,
        # preserving the kwargs the patched code now passes (in particular
        # start_new_session=True).
        return real_popen(
            ["/bin/sh", str(wrapper), str(pidfile)], **kwargs,
        )

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)

    client = CodexAppServerClient(codex_bin="codex")

    # Wait until the grandchild has written its pid.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if pidfile.exists() and pidfile.read_text().strip():
            break
        time.sleep(0.05)
    grandchild_pid = int(pidfile.read_text().strip())
    assert _alive(grandchild_pid), "grandchild didn't start"
    wrapper_pid = client._proc.pid
    assert _alive(wrapper_pid), "wrapper isn't alive"

    # Confirm the spawn used start_new_session — wrapper has its own pgid
    # equal to its own pid (head of the new session).
    assert os.getpgid(wrapper_pid) == wrapper_pid, (
        f"wrapper not in its own session: pgid={os.getpgid(wrapper_pid)} pid={wrapper_pid}"
    )
    # Grandchild inherits the wrapper's pgid.
    assert os.getpgid(grandchild_pid) == wrapper_pid

    # The actual fix under test: close() should reap both.
    client.close(timeout=2.0)

    # Give the kernel a beat to deliver SIGCHLD reaping.
    for _ in range(40):
        if not _alive(wrapper_pid) and not _alive(grandchild_pid):
            break
        time.sleep(0.05)

    assert not _alive(wrapper_pid), "wrapper survived close()"
    assert not _alive(grandchild_pid), (
        f"grandchild orphan survived close() — pgid signalling didn't propagate"
    )


@pytest.mark.live_system_guard_bypass
def test_close_is_idempotent_after_already_dead(monkeypatch):
    """If the subprocess exited before close() runs, close() should be a
    no-op (no exception, no second wait that hangs)."""
    real_popen = subprocess.Popen
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda cmd, **kw: real_popen(["/bin/sh", "-c", "exit 0"], **kw),
    )
    client = CodexAppServerClient(codex_bin="codex")
    # Wait for natural exit.
    client._proc.wait(timeout=2.0)
    client.close(timeout=1.0)
    client.close(timeout=1.0)  # second call must not raise


# ---------- helpers ----------


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False
