"""Plugin slash-command precedence over built-in handlers in cli.HermesCLI.

A plugin that explicitly registers a command name (``ctx.register_command``)
must be able to override the built-in handler for that name (e.g. a richer
plugin ``/kanban`` over the builtin thin wrapper). Without a plugin
registration the built-in dispatch must stay byte-identical.
"""
from unittest.mock import patch

import pytest

from cli import HermesCLI


def _cli():
    c = HermesCLI.__new__(HermesCLI)
    c._pending_resume_sessions = None
    c.session_id = "s1"
    c.config = {}
    return c


def test_builtin_wins_without_plugin_registration():
    """No plugin registers the name: built-in handler dispatches as before."""
    c = _cli()
    with patch.object(HermesCLI, "_toggle_yolo", return_value=None) as m, \
            patch("cli._get_plugin_cmd_handler_names", return_value=set()):
        assert c.process_command("/yolo") is True
        m.assert_called_once_with()


def test_plugin_registered_name_overrides_builtin_handler():
    """A plugin-registered name that also has a builtin handler dispatches to the plugin."""
    c = _cli()
    plugin_commands = {"kanban": {"handler": lambda args: None, "plugin": "hermes-kanban-plugin"}}
    with patch.object(HermesCLI, "_handle_kanban_command") as builtin, \
            patch.object(HermesCLI, "_run_plugin_slash_command") as plugin, \
            patch("cli._get_plugin_cmd_handler_names", return_value={"kanban"}), \
            patch("hermes_cli.plugins.get_plugin_commands", return_value=plugin_commands):
        assert c.process_command("/kanban add task") is True
        builtin.assert_not_called()
        plugin.assert_called_once_with("/kanban", "add task")


def test_plugin_dispatch_returns_true():
    """Override path returns True like a normal handled command (does not exit the REPL)."""
    c = _cli()
    plugin_commands = {"kanban": {"handler": lambda args: None, "plugin": "hermes-kanban-plugin"}}
    with patch.object(HermesCLI, "_run_plugin_slash_command") as plugin, \
            patch("cli._get_plugin_cmd_handler_names", return_value={"kanban"}), \
            patch("hermes_cli.plugins.get_plugin_commands", return_value=plugin_commands):
        assert c.process_command("/kanban") is True
        plugin.assert_called_once_with("/kanban", "")


def test_unknown_name_with_plugin_registration_unchanged():
    """A plugin-registered name with no builtin handler still follows the
    unregistered-slash path (quick_commands -> plugins -> ...), unchanged."""
    c = _cli()
    with patch.object(HermesCLI, "_run_plugin_slash_command") as plugin, \
            patch.object(HermesCLI, "_run_skill_bundle_command") as bundle, \
            patch.object(HermesCLI, "_run_skill_slash_command") as skill, \
            patch.object(HermesCLI, "_expand_slash_prefix") as expand, \
            patch("cli._ensure_skill_commands", return_value={}), \
            patch("cli.get_skill_bundles", return_value={}), \
            patch("cli._get_plugin_cmd_handler_names", return_value={"myplugincmd"}):
        assert c.process_command("/myplugincmd run") is True
        plugin.assert_called_once_with("/myplugincmd", "run")
        bundle.assert_not_called()
        skill.assert_not_called()
        expand.assert_not_called()


@pytest.mark.parametrize("surface_args", [("yolo", None), ("kanban", "handler")])
def test_override_logged(caplog, surface_args):
    """When an override happens, the plugin-vs-builtin dispatch is auditable at INFO."""
    name, kind = surface_args
    c = _cli()
    plugin_commands = {"kanban": {"handler": lambda args: None, "plugin": "hermes-kanban-plugin"}}
    with patch.object(HermesCLI, "_run_plugin_slash_command") as plugin, \
            patch("cli._get_plugin_cmd_handler_names", return_value={"kanban"}), \
            patch("hermes_cli.plugins.get_plugin_commands", return_value=plugin_commands):
        with caplog.at_level("INFO", logger="cli"):
            c.process_command("/kanban args")
    plugin.assert_called_once()
    assert any("Plugin command override: /kanban" in r.message and "hermes-kanban-plugin" in r.message
               for r in caplog.records)
