"""GC of nested per-run worktrees at task completion/archive.

Per-run layout (t_7661c924 contract): ``<repo-root>/.worktrees/<task-id>/<run-id>``
where ``run-id`` is ``tasks.current_run_id``. A failed/timed-out run leaves its
tree behind while the next run materializes a fresh one, so completing (or
archiving / ``kanban gc``-ing) the task must ALSO sweep those orphaned sibling
run trees — under the SAME safety predicates as the primary workspace tree
(clean working tree, every commit reachable from a remote-tracking ref, linked
worktree, never the main checkout, never ``--force``). The task dir
``.worktrees/<task-id>`` itself is removed only when empty, so a preserved
zombie sibling keeps it alive, and a zombie holding the ``wt/<task-id>``
checkout keeps that branch alive (no force branch deletion).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_db_connect as kbc


def _git(*args: str, cwd: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project repo with a remote whose history is fully pushed."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin))
    project = tmp_path / "project"
    _git("clone", str(origin), str(project))
    _git("-C", str(project), "config", "user.email", "t@example.com")
    _git("-C", str(project), "config", "user.name", "t")
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git("-C", str(project), "add", "README.md")
    _git("-C", str(project), "commit", "-m", "init")
    _git("-C", str(project), "push", "origin", "HEAD")
    return project


def _make_run_worktrees(repo: Path, task_id: str) -> tuple[Path, Path]:
    """Materialize the per-run pair: orphaned run 1 + current run 2, both
    checking out the task branch (run 2 needs ``--force`` because the zombie
    run 1 still holds the branch checkout)."""
    task_dir = repo / ".worktrees" / task_id
    run1 = task_dir / "1"
    run2 = task_dir / "2"
    _git("-C", str(repo), "worktree", "add", "-b", f"wt/{task_id}", str(run1))
    _git("-C", str(repo), "worktree", "add", "--force", str(run2), f"wt/{task_id}")
    return run1, run2


def _branch_exists(repo: Path, branch: str) -> bool:
    out = _git("-C", str(repo), "branch", "--list", branch)
    return bool(out.strip())


# ---------------------------------------------------------------------------
# Per-run sweep (nested layout)
# ---------------------------------------------------------------------------


def test_completing_current_run_reaps_orphaned_run_tree_and_task_dir(
    kanban_home: Path, repo: Path
) -> None:
    """Run 1 failed/timed out leaving a clean, fully-pushed tree behind; run 2
    completes — its own tree AND the orphaned run-1 tree go, and the empty
    task dir goes with them."""
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="per-run", assignee="worker")
        run1, run2 = _make_run_worktrees(repo, task_id)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready', workspace_kind='worktree', "
                "workspace_path=?, branch_name=? WHERE id=?",
                (str(run2), f"wt/{task_id}", task_id),
            )
        assert kb.claim_task(conn, task_id, claimer="worker") is not None
        assert kb.complete_task(conn, task_id, summary="run 2 done")
    assert not run2.exists()
    assert not run1.exists()
    assert not (repo / ".worktrees" / task_id).exists()
    # last holder of the branch checkout is gone -> auto branch goes too
    assert not _branch_exists(repo, f"wt/{task_id}")


def test_dirty_orphaned_run_tree_preserved_with_parent_dir(
    kanban_home: Path, repo: Path
) -> None:
    run1, run2 = _make_run_worktrees(repo, "t_dirty223456")
    (run1 / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    kbw._cleanup_worktree_workspace("t_dirty223456", str(run2))
    # current run's tree reaped, zombie's work preserved, task dir kept alive
    assert not run2.exists()
    assert run1.is_dir()
    assert (run1 / "wip.txt").exists()
    assert (repo / ".worktrees" / "t_dirty223456").is_dir()
    # fail-safe branch handling: the zombie still holds the wt/<task-id>
    # checkout, so the branch must survive (never force-deleted)
    assert _branch_exists(repo, "wt/t_dirty223456")


def test_unpushed_orphaned_run_tree_preserved(kanban_home: Path, repo: Path) -> None:
    """The zombie's commit is on a detached HEAD so the shared task branch (and
    with it the current run's tree) stays fully pushed — the predicate must
    preserve the orphan while the pushed primary still goes."""
    run1, run2 = _make_run_worktrees(repo, "t_unpush323456")
    _git("-C", str(run1), "checkout", "--detach", "HEAD")
    (run1 / "work.txt").write_text("committed but not pushed\n", encoding="utf-8")
    _git("-C", str(run1), "add", "work.txt")
    _git("-C", str(run1), "commit", "-m", "local work")
    kbw._cleanup_worktree_workspace("t_unpush323456", str(run2))
    assert not run2.exists()
    assert run1.is_dir()
    assert (run1 / "work.txt").exists()
    assert (repo / ".worktrees" / "t_unpush323456").is_dir()


def test_stray_file_keeps_task_dir_alive(repo: Path) -> None:
    run1, run2 = _make_run_worktrees(repo, "t_stray423456")
    stray = repo / ".worktrees" / "t_stray423456" / "notes.txt"
    stray.write_text("keep me\n", encoding="utf-8")
    kbw._cleanup_worktree_workspace("t_stray423456", str(run2))
    assert not run2.exists()
    assert not run1.exists()
    # rmdir-only semantics: a stray file keeps the task dir alive
    assert (repo / ".worktrees" / "t_stray423456").is_dir()
    assert stray.exists()


def test_other_tasks_worktrees_untouched(repo: Path) -> None:
    """The sweep is scoped to THIS task's dir; sibling task dirs never move."""
    run1, run2 = _make_run_worktrees(repo, "t_mine523456")
    other = repo / ".worktrees" / "t_other523456"
    other_run = other / "1"
    _git("-C", str(repo), "worktree", "add", "-b", "wt/t_other523456", str(other_run))
    kbw._cleanup_worktree_workspace("t_mine523456", str(run2))
    assert not run2.exists()
    assert not run1.exists()
    assert other_run.is_dir()
    assert _branch_exists(repo, "wt/t_other523456")


# ---------------------------------------------------------------------------
# Legacy task-level layout: predicates unchanged
# ---------------------------------------------------------------------------


def test_legacy_layout_clean_tree_removed(repo: Path) -> None:
    """Legacy task-level tree (the worktree IS the task dir) still goes through
    the old path — no nested sweep, no errors, branch deleted."""
    target = repo / ".worktrees" / "t_lega623456"
    kbw._ensure_git_worktree(repo, target, "wt/t_lega623456")
    kbw._cleanup_worktree_workspace("t_lega623456", str(target))
    assert not target.exists()
    assert not _branch_exists(repo, "wt/t_lega623456")


def test_legacy_layout_dirty_tree_preserved(repo: Path) -> None:
    target = repo / ".worktrees" / "t_lega723456"
    kbw._ensure_git_worktree(repo, target, "wt/t_lega723456")
    (target / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    kbw._cleanup_worktree_workspace("t_lega723456", str(target))
    assert target.is_dir()
    assert (target / "wip.txt").exists()


def test_legacy_layout_unpushed_tree_preserved(repo: Path) -> None:
    target = repo / ".worktrees" / "t_lega823456"
    kbw._ensure_git_worktree(repo, target, "wt/t_lega823456")
    (target / "work.txt").write_text("committed but not pushed\n", encoding="utf-8")
    _git("-C", str(target), "add", "work.txt")
    _git("-C", str(target), "commit", "-m", "local work")
    kbw._cleanup_worktree_workspace("t_lega823456", str(target))
    assert target.is_dir()
