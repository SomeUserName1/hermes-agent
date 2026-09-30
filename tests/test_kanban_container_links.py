"""Container links: plan parents must not gate their children (prime pipeline Task 2b).

The completion-gate deadlock (t_f9f6a376): a decomposed parent can only
complete when all children are done (plan gate, spec §8), while task_links
gating refuses to claim/complete a child while the parent is not done
(_parents_satisfied, kanban_db.py). With links as the only edge kind this is
a mutual wait — a cycle. A container parent is a work-breakdown artifact,
not a dependency: its children must be runnable while it waits.

Covers:
  - container-linked child of a blocked parent CAN be claimed
  - dep-linked child of an unfinished parent still CANNOT (regression)
  - child completes while its container parent is blocked
  - link_tasks(kind='container') does not demote a ready child
  - the migration adds ``task_links.kind`` to an existing DB
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


def _mk(conn, title, initial_status="todo", **kwargs):
    """Create a task and pin its status (create_task only accepts
    blocked/running as initial statuses; tests pin via SQL afterwards)."""
    from hermes_cli import kanban_db as kb

    task_id = kb.create_task(conn, title=title, **kwargs)
    conn.execute(
        "UPDATE tasks SET status = ? WHERE id = ?", (initial_status, task_id)
    )
    conn.commit()
    return task_id


def test_container_child_of_blocked_parent_can_be_claimed(kb_conn):
    from hermes_cli import kanban_db as kb

    parent = _mk(kb_conn, "plan parent", initial_status="todo")
    child = _mk(kb_conn, "child")
    kb.link_tasks(kb_conn, parent, child, kind="container")
    # Demote parent to blocked to prove it is the gate that must NOT apply.
    kb_conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (parent,))
    kb_conn.commit()
    kb.recompute_ready(kb_conn)
    claimed = kb.claim_task(kb_conn, child)
    assert claimed is not None, "container parent must not gate the child"


def test_dep_child_of_undone_parent_still_cannot_be_claimed(kb_conn):
    from hermes_cli import kanban_db as kb

    parent = _mk(kb_conn, "dep parent", initial_status="todo")
    child = _mk(kb_conn, "child")
    kb.link_tasks(kb_conn, parent, child)  # default kind='dep'
    kb_conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (parent,))
    kb_conn.commit()
    kb.recompute_ready(kb_conn)
    # The child must not even be promoted to ready.
    status = kb_conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (child,)
    ).fetchone()[0]
    assert status == "todo", "dep parent must gate the child"
    assert kb.claim_task(kb_conn, child) is None


def test_child_completes_while_container_parent_blocked(kb_conn):
    from hermes_cli import kanban_db as kb

    parent = _mk(kb_conn, "plan parent", initial_status="todo")
    child = _mk(kb_conn, "child")
    kb.link_tasks(kb_conn, parent, child, kind="container")
    kb_conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (parent,))
    kb_conn.commit()
    kb.recompute_ready(kb_conn)
    claimed = kb.claim_task(kb_conn, child)
    assert claimed is not None
    ok = kb.complete_task(kb_conn, child, summary="done")
    assert ok, "completion must not refuse because a container parent is open"
    status = kb_conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (child,)
    ).fetchone()[0]
    assert status in ("done", "archived", "review")


def test_container_link_does_not_demote_ready_child(kb_conn):
    from hermes_cli import kanban_db as kb

    parent = _mk(kb_conn, "plan parent", initial_status="todo")
    child = _mk(kb_conn, "child")
    kb.recompute_ready(kb_conn)
    row = kb_conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (child,)
    ).fetchone()
    assert row[0] == "ready"
    kb.link_tasks(kb_conn, parent, child, kind="container")
    row = kb_conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (child,)
    ).fetchone()
    assert row[0] == "ready", "container link must not demote a ready child"
    # Default dep link still demotes (existing behaviour).
    parent2 = _mk(kb_conn, "dep parent", initial_status="todo")
    child2 = _mk(kb_conn, "child2")
    kb.recompute_ready(kb_conn)
    kb.link_tasks(kb_conn, parent2, child2)
    row = kb_conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (child2,)
    ).fetchone()
    assert row[0] == "todo", "dep link must demote a ready child"


def test_link_event_records_kind(kb_conn):
    from hermes_cli import kanban_db as kb

    parent = _mk(kb_conn, "plan parent", initial_status="todo")
    child = _mk(kb_conn, "child")
    kb.link_tasks(kb_conn, parent, child, kind="container")
    body = kb_conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'linked' ORDER BY id DESC LIMIT 1",
        (child,),
    ).fetchone()
    assert body is not None
    assert "container" in body[0], "linked event must show the link kind"


def test_default_links_stored_as_dep(kb_conn):
    from hermes_cli import kanban_db as kb

    parent = _mk(kb_conn, "p", initial_status="todo")
    child = _mk(kb_conn, "c")
    kb.link_tasks(kb_conn, parent, child)
    kind = kb_conn.execute(
        "SELECT kind FROM task_links WHERE parent_id = ? AND child_id = ?",
        (parent, child),
    ).fetchone()[0]
    assert kind == "dep"


def test_migration_adds_kind_to_existing_db(tmp_path):
    from hermes_cli import kanban_db as kb

    db_path = tmp_path / "legacy.db"
    kb.init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        # Recreate a pre-kind task_links, mirroring the legacy schema, and
        # preserve a row so we can assert it survives with kind='dep'.
        conn.executescript(
            """
            ALTER TABLE task_links RENAME TO task_links_old;
            CREATE TABLE task_links (
                parent_id  TEXT NOT NULL,
                child_id   TEXT NOT NULL,
                PRIMARY KEY (parent_id, child_id)
            );
            INSERT INTO task_links VALUES ('legacy-p', 'legacy-c');
            DROP TABLE task_links_old;
            """
        )
        conn.commit()
    finally:
        conn.close()

    kb.init_db(db_path)  # force re-migration

    conn = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(task_links)")}
        assert "kind" in cols
        row = conn.execute(
            "SELECT parent_id, child_id, kind FROM task_links"
        ).fetchone()
        assert row == ("legacy-p", "legacy-c", "dep"), "legacy rows default to dep"
    finally:
        conn.close()


def test_fresh_db_has_kind_column(tmp_path):
    from hermes_cli import kanban_db as kb

    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(task_links)")}
        assert "kind" in cols
    finally:
        conn.close()


def test_promote_ignores_container_parent(kb_conn):
    """promote_task fast-follow: only kind='dep' parents gate promotion."""
    from hermes_cli import kanban_db as kb

    parent = _mk(kb_conn, "container parent", initial_status="todo")
    child = _mk(kb_conn, "child")
    kb.link_tasks(kb_conn, parent, child, kind="container")
    kb_conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (parent,))
    kb_conn.commit()
    ok, err = kb.promote_task(kb_conn, child, actor="test")
    assert ok, f"container parent must not block promotion: {err}"


def test_promote_still_gated_by_dep_parent(kb_conn):
    from hermes_cli import kanban_db as kb

    parent = _mk(kb_conn, "dep parent", initial_status="todo")
    child = _mk(kb_conn, "child")
    kb.link_tasks(kb_conn, parent, child)  # kind='dep'
    kb_conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (parent,))
    kb_conn.commit()
    ok, err = kb.promote_task(kb_conn, child, actor="test")
    assert not ok
    assert "unsatisfied parent dependencies" in err
