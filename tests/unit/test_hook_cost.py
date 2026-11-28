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

import pytest

from subfleet import client as client_module
from subfleet import hooks
from subfleet.client import Client, DaemonError, DaemonUnavailable
from subfleet.daemon import LOOKUP_OPS


def test_a_client_that_does_not_verify_the_lock_asks_ps_nothing(tmp_path, monkeypatch):
    # Always exceed sun_path, so a stale preflight must win before connect.
    root = tmp_path / ("long-" * 24)
    root.mkdir()
    (root / "daemon.lock").write_text(json.dumps({"pid": 12345, "proc_start": "gone"}))
    asked = []
    monkeypatch.setattr(client_module, "_run", lambda argv: asked.append(argv) or (1, ""))
    quiet = Client(root, verify_lock=False)
    try:
        quiet.call("ping", timeout=.5)
    except DaemonUnavailable:
        pass
    assert asked == []                              # refused connects cost no ps either
    loud = Client(root)
    with pytest.raises(DaemonUnavailable, match="stale") as caught:
        loud.call("ping", timeout=.5)
    assert "path too long" not in str(caught.value)
    assert len(asked) == 1 and asked[0][0] == "/bin/ps"


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
    # C-15.6: neither checks the lock. C-16.7: the prompt hook also takes busy at once,
    # since it reads the store offline instead of waiting out busy answers.
    assert built == [{"verify_lock": False}, {"verify_lock": False, "retry_busy": False}]


def test_the_hook_reads_are_the_lookup_ops():
    assert {"list", "show", "notice.pending"} == LOOKUP_OPS
    assert "notice.mark" not in LOOKUP_OPS              # a write waits for the writer, not with the reads


def _refused() -> DaemonUnavailable:
    refused = DaemonUnavailable("refused")
    refused.__cause__ = ConnectionRefusedError(61, "Connection refused")
    return refused


def test_a_hooks_refused_connect_after_busy_asks_about_the_holder_once(tmp_path, monkeypatch):
    """C-16.7: after a busy answer, even an opted-out hook client asks once whether the lock's holder lives.

    C-15.6's opt-out covers a plain refused connect (`unreachable`, tested above), never this case: a dead
    holder after busy is an absent daemon and ends the loop (#232 review r5, P2).
    """
    quiet = Client(tmp_path, verify_lock=False)
    asked = []
    monkeypatch.setattr(quiet, "lock_holder_alive", lambda **kwargs: asked.append(kwargs) or False)
    assert client_module.refused_while_busy(quiet, _refused(), deadline_at=1.0) is False
    assert len(asked) == 1


def test_the_hook_loop_ends_at_once_on_a_dead_holder_after_busy(tmp_path):
    """#232 review r5's executed shape: busy, then refused with a dead holder. The hook ends at once.

    At 1717ee7fc the loop took every such refusal as busy and polled until its deadline (28.6 s of a 30 s budget,
    33 connects, no `ps`); release and this fix ask once and return.
    """
    clock = [0.0]
    calls, asked = [], []

    class Crashed(Client):
        def __init__(self, root):
            super().__init__(root, verify_lock=False)

        def call(self, op, args=None, **_):
            calls.append(op)
            if len(calls) == 1:
                raise DaemonError(69, "the daemon is busy: it holds 512 client connections, its limit",
                                        "try again shortly")
            raise _refused()

        def lock_holder_alive(self, **kwargs):
            asked.append(kwargs)
            return False

    def sleep(seconds):
        clock[0] += seconds

    rc = hooks._wait_and_deliver(Crashed(tmp_path), "s-1", "j-1", 30.0, stderr=None,
                                 now=lambda: clock[0], sleep=sleep)
    assert rc == 0
    assert calls == ["wait", "wait"] and len(asked) == 1
    assert clock[0] < 1.0
