"""PR #96797 kanban policy-veto hooks (pre_kanban_task_claim / pre_kanban_task_complete).

Extracted from :mod:`hermes_cli.kanban_db` so the monolith's code-health ratchet stays
flat: the hook bodies live here, ``kanban_db`` keeps only the call sites. Both hooks are
advisory-by-design (a broken plugin callback never bricks the claim/completion path) and
fail closed on timeout via ``_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS`` (plugins_dispatch.py).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

_log = logging.getLogger("hermes_cli.kanban_db")


def run_pre_kanban_task_claim(
    conn: sqlite3.Connection, task_id: str, claimer: str
) -> bool:
    """Fire ``pre_kanban_task_claim``; True when the claim may proceed.

    A plugin returning ``{"allow": False}`` vetoes the claim (claim_task returns
    None, task stays ``ready``); a raising/broken callback is logged and ignored
    (advisory-by-design). Fail-closed on timeout is owned by the plugin dispatch
    layer's fail-closed timeout set, not here.
    """
    try:
        from hermes_cli.lifecycle import invoke_hook
        hook_results = invoke_hook(
            "pre_kanban_task_claim",
            conn=conn,
            task_id=task_id,
            claimer=claimer,
        )
        for hr in hook_results:
            if isinstance(hr, dict) and hr.get("allow") is False:
                return False
    except Exception as exc:  # health: allow BLE001 -- hook is advisory-by-design: a broken plugin callback must never brick the claim path (PR #96797 contract)
        _log.debug("pre_kanban_task_claim hook error: %s", exc, exc_info=True)
    return True


def run_pre_kanban_task_complete(
    conn: sqlite3.Connection, task_id: str, *, result: Any, summary: Any,
    metadata: Any, created_cards: Any, task_context: str = "",
) -> None:
    """Fire ``pre_kanban_task_complete``; raises PermissionError on a veto.

    Fires AFTER the mechanical gates so a hook sees a mechanically valid
    completion, and BEFORE the final txn so a rejection leaves durable state
    untouched. A veto writes an auditable ``completion_blocked_policy`` event,
    then raises. A raising/broken callback is logged and ignored.
    """
    try:
        from hermes_cli.lifecycle import invoke_hook
        hook_results = invoke_hook(
            "pre_kanban_task_complete",
            conn=conn,
            task_id=task_id,
            result=result,
            summary=summary,
            metadata=metadata,
            created_cards=created_cards,
            task_context=task_context,
        )
        for hr in hook_results:
            if isinstance(hr, dict) and hr.get("allow") is False:
                reason = hr.get("reason", "completion rejected by pre_kanban_task_complete hook")
                from hermes_cli.kanban_db import _append_event, write_txn
                with write_txn(conn):
                    _append_event(conn, task_id, "completion_blocked_policy", {"reason": reason})
                raise PermissionError(reason)
    except PermissionError:
        raise
    except Exception as exc:  # health: allow BLE001 -- hook is advisory-by-design: a broken plugin callback must never brick the completion path (PR #96797 contract)
        _log.debug("pre_kanban_task_complete hook error: %s", exc, exc_info=True)
