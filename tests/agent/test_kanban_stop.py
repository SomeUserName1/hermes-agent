"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_disabled_inside_delegated_child(clear_kanban_env):
    from agent.delegation_context import delegated_child_context

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_parent")

    assert kanban_stop_nudge_enabled() is True
    with delegated_child_context():
        assert kanban_stop_nudge_enabled() is False
        assert build_kanban_stop_nudge(messages=[]) is None
    assert kanban_stop_nudge_enabled() is True


def test_nudge_disabled_inside_non_dispatcher_context(clear_kanban_env):
    from agent.delegation_context import non_dispatcher_owned_context

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_parent")

    assert kanban_stop_nudge_enabled() is True
    with non_dispatcher_owned_context():
        assert kanban_stop_nudge_enabled() is False
        assert build_kanban_stop_nudge(messages=[]) is None
    assert kanban_stop_nudge_enabled() is True


def test_nudge_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.


@pytest.mark.parametrize(
    "tool_name,who",
    [
        ("kanban_request_review", "build worker handing off for same-card review"),
        ("kanban_request_changes", "review agent sending the card back"),
        ("kanban_schedule", "worker parking the card on a timed wait"),
    ],
)
def test_no_nudge_after_handoff_tool(clear_kanban_env, tool_name, who):
    """Handoff tools end the worker's turn just like complete/block.

    Both move the card out of ``running``, and the worker is told to call
    them — goals.py's continuation/finalize prompts name
    ``kanban_request_review``; the force-loaded sdlc-review skill names
    ``kanban_request_changes``. Nudging afterwards asks a worker that did
    the right thing to close a card it must not close.
    """
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_handoff")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": tool_name, "tool_call_id": "1", "content": "ok"},
    ]
    assert session_called_kanban_terminal(messages) is True, who
    assert build_kanban_stop_nudge(messages=messages) is None


def test_nudge_still_fires_for_non_terminal_kanban_tool(clear_kanban_env):
    """Widening the set must not swallow the case the guard exists for."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "Let me open the review next.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_comment", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_comment", "tool_call_id": "1", "content": "ok"},
    ]
    assert session_called_kanban_terminal(messages) is False
    nudge = build_kanban_stop_nudge(messages=messages)
    assert nudge is not None
    # The nudge offers every worker exit, not just close-out; a card that must go
    # through review must never be steered to ``kanban_complete`` alone.
    assert "kanban_request_review" in nudge and "kanban_block" in nudge


# ── Plugin extension of the terminal-tools set ────────────────────────
# Plugins register board tools into the ``kanban`` toolset via
# ``PluginContext.register_tool``; the stop-gate must accept them, but a broken
# plugin stack must never break the guard (fail-open to the builtin set).


@pytest.fixture
def isolated_registry(monkeypatch):
    """A pristine ``tools.registry`` registry so test results don't depend on
    which built-in tools some earlier test registered into the global registry."""
    import tools.registry as registry_mod

    fresh = registry_mod.ToolRegistry()
    monkeypatch.setattr(registry_mod, "registry", fresh)
    return fresh


def _tool_msg(name: str) -> list:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": name, "tool_call_id": "1", "content": "ok"},
    ]


def test_effective_terminal_tools_default_is_builtin(isolated_registry):
    """Without plugin additions the effective set is exactly the builtin set."""
    from agent.kanban_stop import _TERMINAL_KANBAN_TOOLS, _effective_terminal_tools

    assert _effective_terminal_tools() == _TERMINAL_KANBAN_TOOLS


def test_plugin_registered_kanban_tool_satisfies_stop_gate(
    isolated_registry, clear_kanban_env
):
    """A plugin's board handoff tool (registered into the kanban toolset) ends the
    worker's turn just like ``kanban_complete`` — no nudge after it."""
    from tools.registry import registry

    def handler(**kwargs):
        return "handed off"

    registry.register(
        name="kanban_handoff_custom",
        toolset="kanban",
        schema={"type": "object", "properties": {}},
        handler=handler,
    )
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_plugin")

    messages = _tool_msg("kanban_handoff_custom")
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None

    # Widening must not swallow the case the guard exists for.
    nudge = build_kanban_stop_nudge(messages=_tool_msg("kanban_comment"))
    assert nudge is not None


def test_plugin_lookup_failure_fails_open(isolated_registry, monkeypatch, clear_kanban_env):
    """A plugin-stack failure while consulting the registry degrades to the builtin
    set; the stop-gate keeps working."""
    from agent.kanban_stop import _TERMINAL_KANBAN_TOOLS, _effective_terminal_tools

    def boom(toolset):
        raise RuntimeError("plugin stack exploded")

    monkeypatch.setattr(isolated_registry, "get_tool_names_for_toolset", boom)
    assert _effective_terminal_tools() == _TERMINAL_KANBAN_TOOLS
    assert "kanban_complete" in _effective_terminal_tools()

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_fail")
    assert build_kanban_stop_nudge(messages=_tool_msg("kanban_comment")) is not None


def test_registry_import_failure_fails_open(monkeypatch, clear_kanban_env):
    """No tool-registry infra at all → builtin set, guard unaffected."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "tools.registry":
            raise ImportError("no registry in this process")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    from agent.kanban_stop import _TERMINAL_KANBAN_TOOLS, _effective_terminal_tools

    assert _effective_terminal_tools() == _TERMINAL_KANBAN_TOOLS
