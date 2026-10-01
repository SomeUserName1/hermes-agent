"""Per-run worktree resolution: ``<repo>/.worktrees/<task-id>/<run-id>``.

A claimed task carries ``current_run_id``; consecutive runs of one task must
each materialize their OWN worktree under ``.worktrees/<task-id>/<run-id>``
(re-anchored on the main repo, never reusing a persisted path from a previous
run), and a zombie run's branch checkout must not block the next run's
``git worktree add``. Without a run id the legacy task-level layout is kept.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git", "-C", str(cwd),
            "-c", "user.name=Test User",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        check=True, capture_output=True, text=True,
    )
    return result.stdout


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True, capture_output=True, text=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _worktree_task(workspace_path: str | None, run_id: int | None) -> str:
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn,
            title="per-run worktree",
            workspace_kind="worktree",
            workspace_path=workspace_path,
        )
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, tid)
        )
        conn.commit()
    return tid


def _get_task(tid: str):
    with kbc.connect() as conn:
        return kb.get_task(conn, tid)


def _branch_of(path: Path) -> str:
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()


def test_two_consecutive_runs_get_two_trees(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    tid = _worktree_task(str(repo), run_id=479)

    tree1, branch1 = kbw._resolve_worktree_workspace(_get_task(tid))
    assert tree1 == (repo / ".worktrees" / tid / "479").resolve()
    assert branch1 == f"wt/{tid}"
    assert _branch_of(tree1) == f"wt/{tid}"

    # Dispatch persists the resolved path; the NEXT claim bumps current_run_id.
    with kbc.connect() as conn:
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(tree1), tid),
        )
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?", (480, tid)
        )
        conn.commit()

    tree2, branch2 = kbw._resolve_worktree_workspace(_get_task(tid))
    assert tree2 == (repo / ".worktrees" / tid / "480").resolve()
    assert tree2 != tree1
    assert _branch_of(tree2) == f"wt/{tid}"
    assert _branch_of(tree1) == f"wt/{tid}"


def test_zombie_branch_checkout_does_not_block_new_run(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    tid = _worktree_task(str(repo), run_id=2)
    branch = f"wt/{tid}"

    # Run 1's zombie: holds the task branch checkout, never torn down.
    zombie = repo / ".worktrees" / tid / "1"
    _git(repo, "worktree", "add", str(zombie), "-b", branch, "HEAD")
    assert _branch_of(zombie) == branch

    tree2, _ = kbw._resolve_worktree_workspace(_get_task(tid))
    assert tree2 == (repo / ".worktrees" / tid / "2").resolve()
    assert _branch_of(tree2) == branch


def test_same_run_resolution_is_idempotent(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    tid = _worktree_task(str(repo), run_id=7)

    tree1, _ = kbw._resolve_worktree_workspace(_get_task(tid))
    with kbc.connect() as conn:
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(tree1), tid)
        )
        conn.commit()

    before = _git(repo, "worktree", "list", "--porcelain")
    again, branch = kbw._resolve_worktree_workspace(_get_task(tid))
    assert again == tree1
    assert branch == f"wt/{tid}"
    assert _branch_of(again) == f"wt/{tid}"
    assert _git(repo, "worktree", "list", "--porcelain") == before


def test_no_run_id_keeps_legacy_task_level_layout(kanban_home, tmp_path):
    repo = _make_repo(tmp_path)
    tid = _worktree_task(str(repo), run_id=None)

    workspace, branch = kbw._resolve_worktree_workspace(_get_task(tid))
    assert workspace == (repo / ".worktrees" / tid).resolve()
    assert branch == f"wt/{tid}"
    assert _branch_of(workspace) == f"wt/{tid}"


def test_occupied_sibling_path_with_run_id_gets_fresh_per_run_tree(
    kanban_home, tmp_path
):
    repo = _make_repo(tmp_path)
    occupied = repo / ".worktrees" / "sibling"
    _git(repo, "worktree", "add", str(occupied), "-b", "wt/sibling", "HEAD")

    tid = _worktree_task(str(occupied), run_id=5)
    workspace, branch = kbw._resolve_worktree_workspace(_get_task(tid))
    assert workspace == (repo / ".worktrees" / tid / "5").resolve()
    assert branch == f"wt/{tid}"
    assert _branch_of(workspace) == f"wt/{tid}"
    # The sibling's checkout is untouched, still on its own branch.
    assert _branch_of(occupied) == "wt/sibling"


def test_board_default_workdir_anchor_with_run_id(kanban_home, tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    with kbc.connect() as conn:
        kb.write_board_metadata("default", default_workdir=str(repo))
    tid = _worktree_task(None, run_id=11)

    workspace, branch = kbw._resolve_worktree_workspace(_get_task(tid))
    assert workspace == (repo / ".worktrees" / tid / "11").resolve()
    assert branch == f"wt/{tid}"
    assert _branch_of(workspace) == f"wt/{tid}"


def test_anchor_inside_previous_runs_worktree_re_anchors_on_main_repo(
    kanban_home, tmp_path
):
    """A stale anchor INSIDE an old run tree (a linked worktree) must not nest
    the new tree under the old run — re-anchor on the main repo."""
    repo = _make_repo(tmp_path)
    stale = repo / ".worktrees" / "t_other" / "3"
    _git(repo, "worktree", "add", str(stale), "-b", "wt/t_other", "HEAD")

    tid = _worktree_task(str(stale), run_id=4)
    workspace, _ = kbw._resolve_worktree_workspace(_get_task(tid))
    assert workspace == (repo / ".worktrees" / tid / "4").resolve()
