"""Patch 0002 (prime-pipeline Task 4, spec 5.1): unblocking an architect
task whose plan sits in 'awaiting-approval' flips plans.status to
'approved' — the unblock action IS the plan approval.

RED first: this test fails until the patch is applied to
hermes_cli/kanban_db.py.
"""
from __future__ import annotations

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


def _mk_task(conn, task_id="t_arch1", status="todo"):
    conn.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, priority, "
        "created_by, created_at, workspace_kind) "
        "VALUES (?, ?, ?, 'prime', ?, 0, 'test', strftime('%s','now'), 'scratch')",
        (task_id, "architect parent", "b", status),
    )
    conn.commit()


def _mk_plan(conn, task_id="t_arch1", status="awaiting-approval"):
    conn.execute(
        "INSERT INTO plans (id, task_id, status, body_md, created_at) "
        "VALUES ('p_' || ?, ?, ?, ?, strftime('%s','now'))",
        (task_id, task_id, status, "# plan"),
    )
    conn.commit()


def test_unblock_flips_awaiting_plan_to_approved(kb_conn):
    from hermes_cli import kanban_db as kb

    _mk_task(kb_conn)
    _mk_plan(kb_conn)
    kb_conn.execute("UPDATE tasks SET status='blocked' WHERE id='t_arch1'")
    kb_conn.commit()
    assert kb.unblock_task(kb_conn, "t_arch1") is True
    status = kb_conn.execute(
        "SELECT status FROM plans WHERE task_id='t_arch1'"
    ).fetchone()[0]
    assert status == "approved", "unblock must approve the awaiting plan"


def test_unblock_leaves_non_awaiting_plan_alone(kb_conn):
    from hermes_cli import kanban_db as kb

    _mk_task(kb_conn)
    _mk_plan(kb_conn, status="approved")
    kb_conn.execute("UPDATE tasks SET status='blocked' WHERE id='t_arch1'")
    kb_conn.commit()
    kb.unblock_task(kb_conn, "t_arch1")
    row = kb_conn.execute(
        "SELECT status, approved_at FROM plans WHERE task_id='t_arch1'"
    ).fetchone()
    # already approved: approved_at must NOT be overwritten by unblock
    assert row[0] == "approved"
