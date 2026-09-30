"""Pipeline schema migration tests (prime-pipeline Task 1).

Verifies:
  - Fresh ``init_db`` yields the pipeline columns on ``tasks``
    (role, plan_id, forgejo_issue, forgejo_kind), the run-evidence
    columns on ``task_runs`` (review_text, jev_decision, jev_payload),
    and the ``plans`` table.
  - A legacy DB (pre-pipeline ``tasks`` table) is migrated additively
    without losing existing rows.
"""
from __future__ import annotations

import sqlite3

TASK_COLUMNS = ("role", "plan_id", "forgejo_issue", "forgejo_kind")
RUN_COLUMNS = ("review_text", "jev_decision", "jev_payload")


def test_fresh_db_has_pipeline_schema(tmp_path):
    from hermes_cli import kanban_db as kb

    db_path = tmp_path / "kanban.db"
    kb.init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        task_cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        run_cols = {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    for c in TASK_COLUMNS:
        assert c in task_cols, f"tasks.{c} missing on fresh init"
    for c in RUN_COLUMNS:
        assert c in run_cols, f"task_runs.{c} missing on fresh init"
    assert "plans" in tables


def test_legacy_db_migrates_and_preserves_rows(tmp_path):
    from hermes_cli import kanban_db as kb

    db_path = tmp_path / "legacy.db"
    # Build a realistic legacy DB: fresh v1 schema minus the pipeline
    # columns (which postdate it). Dropping via ALTER TABLE keeps every
    # other index/constraint intact so SCHEMA_SQL re-execution succeeds.
    kb.init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        for table, cols in (("tasks", TASK_COLUMNS), ("task_runs", RUN_COLUMNS)):
            for c in cols:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {c}")
        conn.execute("DROP TABLE plans")
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES ('legacy-1', 'old task', 'todo', 1)"
        )
        conn.commit()
    finally:
        conn.close()

    kb.init_db(db_path)

    conn = sqlite3.connect(db_path)
    try:
        task_cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        run_cols = {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")}
        for c in TASK_COLUMNS:
            assert c in task_cols, f"tasks.{c} not added to legacy DB"
        for c in RUN_COLUMNS:
            assert c in run_cols, f"task_runs.{c} not added to legacy DB"
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "plans" in tables
        row = conn.execute(
            "SELECT id, title, status FROM tasks WHERE id = 'legacy-1'"
        ).fetchone()
        assert row == ("legacy-1", "old task", "todo")
    finally:
        conn.close()
