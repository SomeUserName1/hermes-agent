"""Task 5 (prime-pipeline): dispatcher propagates tasks.role to the worker.

The shim reads ``KANBAN_TASK_ROLE`` in main() (Phase A) and routes the run
to the role-specific path (architect = no worktree, etc.). But the
dispatcher's ``_default_spawn`` built the worker env without it, and
``Task.from_row`` never surfaced the pipeline columns at all — so a task
created with role='architect' would still run as a legacy implementer.
Deterministic wiring, not prompt-based hope: the role must ride the env.
"""
from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture()
def kb_conn(tmp_path):
    from hermes_cli import kanban_db as kb

    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    conn = kb.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def _mk_task(conn, role=None, assignee="prime"):
    conn.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, priority, "
        "created_by, created_at, workspace_kind, role) "
        "VALUES (?, ?, ?, ?, 'ready', 0, 'test', strftime('%s','now'), 'scratch', ?)",
        ("t_testrole", "test task", "body", assignee, role),
    )
    conn.commit()
    from hermes_cli import kanban_db as kb

    return kb.get_task(conn, "t_testrole")


def test_task_from_row_surfaces_role(kb_conn):
    from hermes_cli import kanban_db as kb

    task = _mk_task(kb_conn, role="architect")
    assert task.role == "architect"


def test_task_from_row_role_none_when_absent(kb_conn):
    """role=None when the column value is NULL (legacy rows)."""
    from hermes_cli import kanban_db as kb

    task = _mk_task(kb_conn, role=None)
    assert task.role is None


def test_default_spawn_sets_kanban_task_role(kb_conn, monkeypatch, tmp_path):
    """_default_spawn must inject KANBAN_TASK_ROLE from tasks.role."""
    import os

    from hermes_cli import kanban_db as kb

    task = _mk_task(kb_conn, role="architect")
    task.workspace_path = str(tmp_path / "ws")
    os.makedirs(task.workspace_path, exist_ok=True)

    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env") or {}
            captured["start_new_session"] = kwargs.get("start_new_session")
            self._pid = 424242

        @property
        def pid(self):
            return self._pid

    import subprocess

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    monkeypatch.setattr(kb, "kanban_db_path", lambda board=None: str(tmp_path / "k.db"))
    monkeypatch.setattr(kb, "workspaces_root", lambda board=None: str(tmp_path))
    monkeypatch.setattr(kb, "_retag_legacy_worker_sessions", lambda *a, **k: None)
    monkeypatch.setattr(kb, "get_current_board", lambda: "test")
    monkeypatch.setattr(
        kb, "_resolve_hermes_argv", lambda: ["hermes"]
    )
    monkeypatch.setattr(
        kb, "_resolve_worker_cli_toolsets", lambda home: []
    )
    monkeypatch.setattr(
        kb, "_worker_terminal_timeout_env", lambda a, b: None
    )
    from gateway import session_context  # noqa: F401  (import used inside fn)

    pid = kb._default_spawn(task, task.workspace_path, board="test")
    assert pid == 424242
    env = captured["env"]
    assert env.get("KANBAN_TASK_ROLE") == "architect"
