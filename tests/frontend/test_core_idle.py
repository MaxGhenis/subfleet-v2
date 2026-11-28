"""C-16.7 with the app's own transport: its long polls outlast the idle close.

The daemon closes a connection that has sent nothing and received no reply for
`connection_idle_s` (60 s) while nothing is outstanding. The app sends one
request per connection and waits `wait_s + 15` s for a long poll's answer
(`app/Sources/DaemonClient.swift`, design §12). Here the app's `DaemonClient`
(compiled into the core probe) calls a real daemon core whose idle close is 1 s
with polls of 3 s: each is answered at its deadline, never cut by the close.
"""

from __future__ import annotations

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.unit.test_daemon_connections import counts, serve  # noqa: F401 - serve: a fixture

pytestmark = needs_swift

SETTINGS = {"model": "opus", "effort": None, "fast": False, "permission": "ask", "auto_continue": True}


def test_c16_7_the_apps_long_polls_are_answered_through_a_short_idle_close(core_probe, tmp_path, serve):
    service = serve(connection_idle_s=1)
    created, _ = service.conversations.store.create_conversation(
        provider="claude", workspace="/tmp", workspace_kind="in-place", settings=SETTINGS, origin="new")
    socket_path = service.root / "daemon.sock"
    polls = {
        "conversation.watch": {"after": 10**9, "wait_s": 3},
        "conversation.events": {"conversation_id": created["conversation_id"], "after": 10**9, "wait_s": 3},
    }
    for op, args in polls.items():
        answer = run_probe(core_probe, "call", socket_path, op, write_json(tmp_path / f"{op}.json", args))
        assert "ok" in answer, (op, answer)
        assert 2.5 <= answer["elapsed"] < 10, (op, answer)
    assert counts(service)["abandoned"] == 0
