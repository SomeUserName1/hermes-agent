"""Nested per-run kanban worktree layout: <repo>/.worktrees/<task-id>/<run-id>.

Contracts (kanban t_17304e24):

- cli._prune_stale_worktrees descends into t_<hex> task dirs and age-classifies
  each RUN tree like a scratch tree (24h soft / 72h hard) with the usual
  safety predicates: stale clean merged run trees reaped; dirty, unpushed and
  live-locked run trees preserved; the task dir itself removed (rmdir-only)
  only once it holds no run trees.
- Legacy flat t_<hex> trees (pre nested-runs layout, the task entry IS the
  worktree) keep today's skip behavior.
- worktree_gc.audit_worktrees lists a TreeRecord per run tree and keeps the
  "kanban task tree (owned by kanban gc)" verdict for the task dir itself;
  worktrees_summary counts per-run trees.

Every test drives a REAL git repo fixture (origin bare + clone) — no mocks.
"""

import os
import subprocess
import time

import pytest

from hermes_cli import worktree_gc
import cli


def _git(args, cwd, env=None):
    e = dict(os.environ)
    e.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    })
    if env:
        e.update(env)
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=str(cwd), env=e,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """origin (bare) + clone with .worktrees/, HOME redirected for archives."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()

    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(["init", "--bare", "-b", "main"], origin)

    clone = tmp_path / "repo"
    _git(["clone", str(origin), str(clone)], tmp_path)
    (clone / "README.md").write_text("hello\n")
    _git(["add", "."], clone)
    _git(["commit", "-m", "init"], clone)
    _git(["push", "origin", "main"], clone)
    _git(["remote", "set-head", "origin", "main"], clone)
    (clone / ".worktrees").mkdir()
    return clone


def _add_run_tree(repo_path, task_id, run_id, branch=None):
    """Nested per-run layout: .worktrees/<task>/<run> on the task branch.

    Every per-run tree checks out the SAME task branch (wt/<task-id>); later
    runs need `git worktree add --force` because a zombie run tree from run N
    still holds the branch checkout.
    """
    task_dir = repo_path / ".worktrees" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    tree = task_dir / str(run_id)
    branch = branch or f"wt/{task_id}"
    if _branch_exists(repo_path, branch):
        _git(["worktree", "add", "--force", str(tree), branch], repo_path)
    else:
        _git(["worktree", "add", str(tree), "-b", branch], repo_path)
    return tree, branch


def _branch_exists(repo_path, branch):
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", branch],
        capture_output=True, text=True, cwd=str(repo_path),
    )
    return probe.returncode == 0


def _age(path, hours):
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))


class TestStartupPrunerNestedRuns:
    def test_stale_run_trees_classified_and_task_dir_removed_when_empty(self, repo):
        """Stale clean merged run tree reaped; task dir rmdir'd once empty."""
        tree, branch = _add_run_tree(repo, "t_deadbeef", 481)
        _age(tree, 25)  # past the 24h soft tier
        cli._prune_stale_worktrees(str(repo), max_age_hours=24)
        assert not tree.exists(), "stale clean merged run tree must be reaped"
        assert not (repo / ".worktrees" / "t_deadbeef").exists(), (
            "task dir must be rmdir'd once it holds no run trees"
        )
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", branch],
            capture_output=True, text=True, cwd=str(repo),
        )
        assert probe.returncode != 0, "reaped scratch-equivalent run tree loses its branch"

    def test_dirty_unpushed_live_locked_preserved_and_task_dir_kept(self, repo):
        task_dir = repo / ".worktrees" / "t_deadbeef"
        dirty, _ = _add_run_tree(repo, "t_deadbeef", 481)
        (dirty / "README.md").write_text("edited\n")
        _age(dirty, 80)

        unpushed, _ = _add_run_tree(repo, "t_deadbeef", 482)
        (unpushed / "work.py").write_text("x = 1\n")
        _git(["add", "."], unpushed)
        _git(["commit", "-m", "unique work"], unpushed)
        _age(unpushed, 80)

        live, _ = _add_run_tree(repo, "t_deadbeef", 483)
        _git(["worktree", "lock", str(live),
              "--reason", f"hermes pid={os.getpid()}"], repo)
        _age(live, 80)

        cli._prune_stale_worktrees(str(repo), max_age_hours=24)

        assert dirty.exists(), "dirty run tree must be preserved"
        assert unpushed.exists(), "unpushed run tree must be preserved"
        assert live.exists(), "live-locked run tree must be preserved"
        assert task_dir.exists(), (
            "task dir must stay while it still contains run trees"
        )

    def test_young_run_tree_skipped(self, repo):
        tree, _ = _add_run_tree(repo, "t_deadbeef", 481)
        _age(tree, 2)  # under the 24h soft tier
        cli._prune_stale_worktrees(str(repo), max_age_hours=24)
        assert tree.exists(), "young run tree must be skipped"


class TestLegacyFlatTrees:
    def test_flat_kanban_tree_still_skipped_by_startup_pruner(self, repo):
        """Pre-change layout: .worktrees/t_<hex> IS the worktree — owned by
        the kanban dispatcher's gc, never touched by the startup pruner."""
        tree = repo / ".worktrees" / "t_c0ffee"
        _git(["worktree", "add", str(tree), "-b", "kanban/t_c0ffee"], repo)
        (tree / "scratch.py").write_text("kept = True\n")
        _git(["add", "."], tree)
        _git(["commit", "-m", "kanban wip"], tree)
        _age(tree, 30 * 24)

        cli._prune_stale_worktrees(str(repo), max_age_hours=24)

        assert tree.exists(), "legacy flat kanban tree keeps its skip behavior"

    def test_flat_kanban_tree_keeps_audit_verdict(self, repo):
        tree = repo / ".worktrees" / "t_c0ffee"
        _git(["worktree", "add", str(tree), "-b", "kanban/t_c0ffee"], repo)
        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        match = [r for r in records if r.name == "t_c0ffee"]
        assert match, "legacy flat tree must still be listed"
        assert match[0].verdict == "keep"
        assert "kanban task tree" in match[0].reason


class TestAuditAndSummaryNested:
    def test_audit_lists_run_trees_and_task_dir_verdict(self, repo):
        dirty, _ = _add_run_tree(repo, "t_deadbeef", 481)
        (dirty / "README.md").write_text("edited\n")
        clean, _ = _add_run_tree(repo, "t_deadbeef", 482)

        records = worktree_gc.audit_worktrees(str(repo), with_sizes=False)
        by_name = {r.name: r for r in records}

        run1 = by_name["t_deadbeef/481"]
        assert run1.verdict == "keep"
        assert "tracked" in run1.reason
        run2 = by_name["t_deadbeef/482"]
        assert run2.verdict == "reap"
        task = by_name["t_deadbeef"]
        assert task.verdict == "keep"
        assert task.reason == "kanban task tree (owned by kanban gc)"

    def test_summary_counts_run_trees_not_task_dirs(self, repo):
        _add_run_tree(repo, "t_deadbeef", 481)
        _add_run_tree(repo, "t_deadbeef", 482)
        count, _ = worktree_gc.worktrees_summary(str(repo))
        assert count == 2, "per-run trees counted, not the task dir"
