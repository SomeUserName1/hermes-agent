"""Dispatch-path coverage for the PR #96797 contribution: the pre_kanban_task_claim
veto must hold inside ``dispatch_once`` (not just direct ``claim_task`` calls), and a
mechanically-valid completion must still be vetoable by ``pre_kanban_task_complete``
after the created-cards / empty-completion gates pass."""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import plugins


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home, all_assignees_spawnable):
    with kbc.connect() as c:
        yield c


def _register_hook(name, callback):
    mgr = plugins.get_plugin_manager()
    with mgr._discovery_lock:
        mgr._hooks.setdefault(name, []).append(callback)
    return mgr


def _drop_hook(name, callback):
    mgr = plugins.get_plugin_manager()
    with mgr._discovery_lock:
        if callback in mgr._hooks.get(name, []):
            mgr._hooks[name].remove(callback)


def test_dispatch_once_respects_pre_claim_veto(conn):
    """A pre_kanban_task_claim veto must stop the dispatcher's claim of the task:
    dispatch_once reports the tick as not-spawned and the card stays 'ready'."""
    task_id = kb.create_task(conn, title="vetoed card", assignee="worker")

    def _veto_claim(**kwargs):
        if kwargs.get("task_id") == task_id:
            return {"allow": False, "reason": "policy hold"}
        return None

    _register_hook("pre_kanban_task_claim", _veto_claim)
    spawn_calls: list = []

    def spy_spawn(task, workspace_path, board=None):
        spawn_calls.append(getattr(task, "id", task))
        return 999999

    try:
        result = kbd.dispatch_once(conn, spawn_fn=spy_spawn)
    finally:
        _drop_hook("pre_kanban_task_claim", _veto_claim)

    assert task_id not in result.spawned
    assert spawn_calls == []
    assert kb.get_task(conn, task_id).status == "ready"

    # The veto is a policy HOLD, not a block: lifting it lets the next tick claim + spawn.
    result2 = kbd.dispatch_once(conn, spawn_fn=spy_spawn)
    assert any(row[0] == task_id for row in result2.spawned)
    assert kb.get_task(conn, task_id).status == "running"


def test_pre_complete_veto_fires_after_mechanical_gates_pass(conn):
    """The completion veto sees a mechanically VALID completion: the created-cards
    and empty-completion gates pass first, then the hook vetoes — and the veto is
    auditable (completion_blocked_policy event) with durable state untouched."""
    task_id = kb.create_task(conn, title="gated card", assignee="worker")
    kb.claim_task(conn, task_id)

    vetoed: list = []

    def _veto_complete(**kwargs):
        if kwargs.get("task_id") == task_id:
            vetoed.append(kwargs.get("result"))
            return {"allow": False, "reason": "evidence bundle missing"}
        return None

    _register_hook("pre_kanban_task_complete", _veto_complete)
    try:
        # Non-empty result => the empty-completion gate passes; no cards => the cards gate passes.
        with pytest.raises(PermissionError, match="evidence bundle missing"):
            kb.complete_task(conn, task_id, result="done, but policy says no")
    finally:
        _drop_hook("pre_kanban_task_complete", _veto_complete)

    assert vetoed == ["done, but policy says no"]  # hook saw the real result payload
    assert kb.get_task(conn, task_id).status == "running"

    events = kb.list_events(conn, task_id)
    assert any(e.kind == "completion_blocked_policy" for e in events)
