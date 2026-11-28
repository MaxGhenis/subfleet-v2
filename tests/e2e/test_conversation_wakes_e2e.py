"""Real daemon, relay, fake provider and wake CLI; all children reaped by e2e."""
import json
import shlex
import sys
from datetime import UTC, datetime, timedelta

import pytest

from tests.e2e.test_conversations import Conversations

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="LOCAL_PEERPID is macOS")


def setup(e2e, command, *, final=False):
    e2e.env["SUBFLEET_FAKE_TURN_LOG"] = str(e2e.root / "turns.jsonl")
    e2e.env["SUBFLEET_FAKE_BASH_COMMAND"] = command
    if final:
        e2e.env["SUBFLEET_FAKE_BASH_WAKE"] = "1"
    clock = e2e.root / "wake-clock"
    clock.write_text("0")
    e2e.env["SUBFLEET_FAKE_WAKE_CLOCK"] = str(clock)
    e2e.start()
    conv = Conversations(e2e)
    cid = conv.create()
    mid = conv.submit(cid, "dispatch and check back [fake:bash]")
    assert conv.until_state(mid, "complete", "failed", "delivery-unknown", timeout=60)["state"] == "complete"
    return conv, cid, clock


def one_wake(conv, cid):
    def found():
        ms = conv.call("conversation.open", conversation_id=cid)["messages"]
        wake = [m for m in ms if m["origin"] == "wake"]
        return wake[0] if len(wake) == 1 else None
    message = conv.e2e.until(found, timeout=60)
    assert conv.until_state(message["message_id"], "complete", "failed", "delivery-unknown", timeout=60)["state"] == "complete"
    assert len([m for m in conv.call("conversation.open", conversation_id=cid)["messages"] if m["origin"] == "wake"]) == 1
    assert message["text"].startswith("[Subfleet]")
    return message


@pytest.mark.parametrize("final", [False, True])
def test_turn_dispatches_run_ends_and_completion_starts_exactly_one_new_turn(e2e, final):
    command = shlex.join([sys.executable, "-m", "subfleet.cli", *e2e.run_args("astra", "--name", "wake-run")])
    conv, cid, _ = setup(e2e, command, final=final)
    message = one_wake(conv, cid)
    ran = [r for r in conv.turn_log() if "bash" in r]
    assert len(ran) == 1 and ran[0]["rc"] == 0
    assert ran[0]["stdout"].strip() + " finished: succeeded; deliverable " in message["text"]
    if final:
        assert "Inspect the finished run" in message["text"]


def test_cli_at_reinvokes_after_five_minutes(e2e):
    at = (datetime.now(UTC) + timedelta(minutes=6)).isoformat()
    # Use the installed permanent front door, including compatibility parsing.
    command = shlex.join([sys.executable, "-c", "from subfleet.compat import dispatch; raise SystemExit(dispatch())",
                         "wake", "--at", at, "--note", "Inspect progress"])
    conv, cid, clock = setup(e2e, command)
    assert not [m for m in conv.call("conversation.open", conversation_id=cid)["messages"] if m["origin"] == "wake"]
    clock.write_text("361")
    assert "Inspect progress" in one_wake(conv, cid)["text"]


def test_cli_pr_event_uses_batched_gh_and_reinvokes(e2e):
    state = e2e.root / "pr-state"
    state.write_text("OPEN")
    gh = e2e.root / "bin/gh"
    gh.write_text(f'''#!{sys.executable}
import json, sys
from pathlib import Path
body = json.load(sys.stdin)
Path({str(e2e.root / "gh-query")!r}).write_text(body["query"])
state = Path({str(state)!r}).read_text()
print(json.dumps({{"data": {{"p0": {{"pullRequest": {{"state": state, "headRefOid": "h", "reviews": {{"nodes": []}},
  "statusCheckRollup": {{"contexts": {{"nodes": [], "pageInfo": {{"hasNextPage": False}}}}}}}}}}}}}}))
''')
    gh.chmod(0o700)
    command = shlex.join([sys.executable, "-m", "subfleet.cli", "wake", "--pr", "owner/repo#1", "--note", "Deliver the merge"])
    conv, cid, clock = setup(e2e, command)
    e2e.until(lambda: (e2e.root / "gh-query").exists(), timeout=30)
    state.write_text("MERGED")
    clock.write_text("61")
    assert "owner/repo#1" in one_wake(conv, cid)["text"]
