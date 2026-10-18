"""Review r3 of PR #127: the P1-2 strand with its timing recorded.

Same scenario as tests/e2e/test_review_r2_strand_e2e.py, but it proves the strand was
exercised (the background waiter returned after the turn's `result` and before the turn
process exited). It keeps the turn log and timestamps under the test's own state root
(`<root>/evidence/`), never in the checkout; set SUBFLEET_R3_EVIDENCE to a folder to keep them."""
import json
import os
import shlex
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from tests.e2e.test_conversations import Conversations

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="LOCAL_PEERPID is macOS")

TERMINAL = ("succeeded", "failed", "cancelled", "lost", "quarantined")


@pytest.mark.parametrize("final", [False, True], ids=["automatic", "wake-me"])
def test_strand_timing(e2e, final):
    release = e2e.root / "release-run"
    e2e.env["SUBFLEET_FAKE_RELEASE_PATH"] = str(release)
    e2e.env["SUBFLEET_FAKE_TURN_LOG"] = str(e2e.root / "turns.jsonl")
    e2e.env["SUBFLEET_FAKE_BASH_COMMAND"] = shlex.join(
        [sys.executable, "-m", "subfleet.cli", *e2e.run_args("astra", "--name", "strand-run")])
    # Stamp when the waiter returns, in the same clock as the stores (UTC ISO).
    # The fake appends the run id, so this is a function call: f <id>.
    e2e.env["SUBFLEET_FAKE_BG_COMMAND"] = (
        "f() { " + shlex.join([sys.executable, "-m", "subfleet.cli", "wait"]) + ' "$1" 2>&1; rc=$?; '
        "date -u +WAITER_RETURNED_AT=%Y-%m-%dT%H:%M:%SZ; return $rc; }; f")
    if final:
        e2e.env["SUBFLEET_FAKE_BASH_WAKE"] = "1"
    clock = e2e.root / "wake-clock"
    clock.write_text("0")
    e2e.env["SUBFLEET_FAKE_WAKE_CLOCK"] = str(clock)
    e2e.start()
    conv = Conversations(e2e)
    cid = conv.create()
    mid = conv.submit(cid, "dispatch, arm a background wait, end the turn [fake:bash]")
    ran = e2e.until(lambda: [r for r in conv.turn_log() if "bash" in r], timeout=60)
    job_id = ran[0]["stdout"].strip()
    assert ran[0]["rc"] == 0 and job_id, ran
    time.sleep(1.5)                                   # the result frame is written before the run is released
    release.write_text("go")
    assert conv.until_state(mid, "complete", "failed", "delivery-unknown", timeout=90)["state"] == "complete"
    background = e2e.until(lambda: [r for r in conv.turn_log() if "background" in r], timeout=90)
    e2e.until(lambda: e2e.job(job_id)["state"] in TERMINAL, timeout=60)
    e2e.until(lambda: all(j["state"] in TERMINAL for j in e2e.rows("SELECT state FROM jobs WHERE kind='turn'")),
              timeout=60)
    deadline, wakes = time.monotonic() + 20, []
    while time.monotonic() < deadline and not wakes:
        wakes = [m for m in conv.call("conversation.open", conversation_id=cid)["messages"] if m["origin"] == "wake"]
        time.sleep(0.25)
    run = e2e.rows("SELECT job_id,state,created_at,finished_at FROM jobs WHERE job_id=?", (job_id,))[0]
    turn = e2e.rows("SELECT job_id,state,created_at,finished_at FROM jobs WHERE kind='turn' ORDER BY created_at")
    notices = e2e.rows("SELECT state,transport,acknowledged_at,offered_at FROM notices WHERE job_id=?", (job_id,))
    with sqlite3.connect(f"file:{e2e.root / 'conversations.sqlite3'}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        messages = [dict(r) for r in db.execute(
            "SELECT message_id,origin,state,created_at FROM messages WHERE conversation_id=? ORDER BY seq", (cid,))]
        requests = [dict(r) for r in db.execute(
            "SELECT kind,state,created_at FROM wake_requests WHERE conversation_id=?", (cid,))]
    stdout = background[0]["stdout"] + background[0].get("stderr", "")
    returned = [line.split("=", 1)[1] for line in stdout.splitlines() if line.startswith("WAITER_RETURNED_AT=")]
    record = {"case": "wake-me" if final else "automatic", "run": run, "turn_jobs": turn, "notices": notices,
              "messages": messages, "requests": requests, "waiter": background[0], "waiter_returned_at": returned,
              "turn_log": conv.turn_log()}
    evidence = Path(os.environ.get("SUBFLEET_R3_EVIDENCE") or e2e.root / "evidence")
    evidence.mkdir(parents=True, exist_ok=True)
    out = evidence / f"strand-timing-{record['case']}.json"
    out.write_text(json.dumps(record, indent=2, default=str))
    for name in ("daemon.log",):
        for path in e2e.root.rglob(name):
            shutil.copy(path, evidence / f"strand-timing-{record['case']}-{name}")
            break
    print(f"R3 strand ({record['case']}): wakes={len(wakes)} waiter_rc={background[0]['rc']} "
          f"waiter_returned={returned} run_finished={run['finished_at']} turn_job_finished="
          f"{[t['finished_at'] for t in turn]} notice={notices} wake_created="
          f"{[m['created_at'] for m in messages if m['origin'] == 'wake']}")
    assert background[0]["rc"] == 0 and returned
    # The strand is exercised only if the run finished, and the waiter returned, while the
    # turn job was still live (after its result frame: the message was complete by then).
    assert run["finished_at"] <= turn[0]["finished_at"] and returned[0] <= turn[0]["finished_at"]
    assert wakes, f"never woken: notice {notices}, requests {requests}"
