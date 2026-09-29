"""Process-group kill tests for kanban worker termination (t_a2b73dfd).

Workers are spawned with ``start_new_session=True`` (PGID == PID), so
timeout/reclaim paths must kill the whole process group — not just the
top PID — otherwise the agent kernel's spawned children (bash tool
subprocesses etc.) survive as orphans and keep writing in the task
worktree.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from hermes_cli import kanban_db as kb

PARENT_SCRIPT = textwrap.dedent(
    """
    import subprocess, sys, time
    # Spawn a grandchild that inherits the parent's process group, then
    # report its PID so the test can verify it dies with the group.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    print(child.pid, flush=True)
    time.sleep(600)
    """
)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pid: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.05)
    pytest.fail(f"pid {pid} survived the group kill")


@pytest.fixture
def process_tree():
    """Spawn parent+grandchild fixture tree; parent leads its own group."""
    proc = subprocess.Popen(
        [sys.executable, "-c", PARENT_SCRIPT],
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        grandchild_pid = int(proc.stdout.readline().strip())
        # Sanity: parent leads its own group and the grandchild is in it.
        assert os.getpgid(proc.pid) == proc.pid
        assert os.getpgid(grandchild_pid) == proc.pid
        yield proc, grandchild_pid
    finally:
        # Cleanup: take down the whole tree in case asserts fired early.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        proc.wait(timeout=10)


# Real signal delivery to a self-spawned fixture tree is the point of
# these tests — bypass the conftest live-system guard.
@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(not hasattr(os, "killpg"), reason="requires os.killpg")
def test_group_kill_takes_down_whole_process_tree(process_tree):
    """Killing the worker PGID kills the worker AND its descendants."""
    proc, grandchild_pid = process_tree
    kill = kb._worker_kill_fn()
    kill(proc.pid, signal.SIGTERM)
    proc.wait(timeout=10)
    _wait_gone(grandchild_pid)


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(not hasattr(os, "killpg"), reason="requires os.killpg")
def test_worker_kill_fn_default_is_group_kill(process_tree):
    """The default kill fn signals the process group (pgid == worker pid),
    which the kernel delivers to every member of the group."""
    proc, grandchild_pid = process_tree
    kill = kb._worker_kill_fn()
    assert kill is not None
    assert kill is not kb.os.kill  # not the plain PID kill
    kill(proc.pid, signal.SIGTERM)
    proc.wait(timeout=10)
    _wait_gone(grandchild_pid)


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(not hasattr(os, "killpg"), reason="requires os.killpg")
def test_reclaimed_worker_kill_reaches_grandchild(process_tree):
    """_terminate_reclaimed_worker (stale/reclaim path) also kills the
    whole tree: no surviving descendant outlives the worker."""
    proc, grandchild_pid = process_tree
    claim_lock = f"{kb._claimer_id().split(':', 1)[0]}:12345"
    info = kb._terminate_reclaimed_worker(
        proc.pid, claim_lock, signal_fn=None,
    )
    assert info["host_local"] is True
    assert info["termination_attempted"] is True
    proc.wait(timeout=10)
    _wait_gone(grandchild_pid)


def test_signal_fn_hook_receives_group_kill(monkeypatch):
    """The signal_fn test hook receives the group kill as (pgid, sig),
    with pgid == the recorded worker pid."""
    calls = []
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    def hook(pid, sig):
        calls.append((pid, sig))

    kill = kb._worker_kill_fn(hook)
    assert kill is hook
    kill(4242, signal.SIGTERM)
    assert calls == [(4242, signal.SIGTERM)]


def test_kill_fn_falls_back_to_pid_kill(monkeypatch):
    """Without os.killpg (Windows) the default kill fn degrades to
    os.kill; when killpg reports the group gone it retries via os.kill
    before giving up."""
    pid_kills = []

    def fake_kill(pid, sig):
        pid_kills.append((pid, sig))

    monkeypatch.setattr(kb.os, "kill", fake_kill)

    # (a) Windows-like platform: no killpg at all.
    import builtins
    real_hasattr = builtins.hasattr

    def fake_hasattr(obj, name):
        if name == "killpg":
            return False
        return real_hasattr(obj, name)

    real_killpg = getattr(kb.os, "killpg", None)
    if real_killpg is not None:
        monkeypatch.delattr(kb.os, "killpg")
    try:
        kill = kb._worker_kill_fn()
        assert kill is fake_kill
    finally:
        if real_killpg is not None:
            setattr(kb.os, "killpg", real_killpg)

    # (b) killpg present but the group is gone -> os.kill fallback.
    def fake_killpg(pgid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(kb.os, "killpg", fake_killpg)
    kill = kb._worker_kill_fn()
    kill(4242, signal.SIGTERM)
    assert pid_kills == [(4242, signal.SIGTERM)]


def test_kill_fn_falls_back_to_none_without_kill_support(monkeypatch):
    """A platform with neither killpg nor kill yields None (callers treat
    that as 'cannot signal', matching the previous behaviour)."""
    real_kill = getattr(kb.os, "kill", None)
    real_killpg = getattr(kb.os, "killpg", None)
    monkeypatch.delattr(kb.os, "kill", raising=False)
    monkeypatch.delattr(kb.os, "killpg", raising=False)
    try:
        assert kb._worker_kill_fn() is None
    finally:
        if real_kill is not None:
            setattr(kb.os, "kill", real_kill)
        if real_killpg is not None:
            setattr(kb.os, "killpg", real_killpg)
