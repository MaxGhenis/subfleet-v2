"""C-15.6: what a hook costs the machine and the daemon.

Every Bash call of every Claude session runs the PostToolUse hook, and every
prompt runs UserPromptSubmit. The hooks said nothing different whichever way a
daemon was down, yet each process checked the daemon's lock first, with a `ps`
and a `sysctl`: two forks per Bash call, machine-wide, on a machine already at
load 80-100. On the daemon, the hook's `list`, `show` and `notice.pending` read
the store off its lock (C-3.7) on a pool of their own (C-16.5).
"""

from __future__ import annotations

import json

from subfleet import client as client_module
from subfleet import hooks
from subfleet.client import Client, DaemonUnavailable
from subfleet.daemon import LOOKUP_OPS


def test_a_client_that_does_not_verify_the_lock_asks_ps_nothing(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(Client, "lock_report", lambda self: asked.append(1) or (False, "dead"))
    quiet = Client(tmp_path, verify_lock=False)
    try:
        quiet.call("ping", timeout=.5)
    except DaemonUnavailable:
        pass                                            # nothing listens: the connect says so
    assert asked == []
    loud = Client(tmp_path)
    try:
        loud.call("ping", timeout=.5)
    except DaemonUnavailable as exc:
        assert "stale" in str(exc)                      # the default still checks the lock first
    assert asked == [1]


def test_the_hooks_build_their_clients_without_the_lock_check(tmp_path, monkeypatch):
    built = []

    class Recording:
        def __init__(self, root, **options):
            built.append(options)

        def call(self, op, args=None, **_):
            return {"jobs": []} if op == "list" else {"notices": []}
    monkeypatch.setattr(hooks, "Client", Recording)
    payload = {"session_id": "s-1", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert hooks.post_tool_use(payload, tmp_path, budget_s=1) == 0
    assert hooks.session_event("UserPromptSubmit", payload, tmp_path, stdout=open("/dev/null", "w"),
                               env={}) == 0
    assert built == [{"verify_lock": False}, {"verify_lock": False}]


def test_the_hook_reads_are_the_lookup_ops():
    assert {"list", "show", "notice.pending"} == LOOKUP_OPS
    assert "notice.mark" not in LOOKUP_OPS              # a write waits for the writer, not with the reads
