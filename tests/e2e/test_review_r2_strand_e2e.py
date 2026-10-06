"""Review r2 of PR #127 (not for merge): a background `subfleet wait` the turn armed
returns after the turn's `result` (D-15 keeps Claude -p alive for it), acknowledges
the run's notice, and the conversation is never woken. Real daemon, CLI and relay."""
import shlex
import sqlite3
import sys
import time

import pytest

from tests.e2e.test_conversations import Conversations

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="LOCAL_PEERPID is macOS")

TERMINAL = ("succeeded", "failed", "cancelled", "lost", "quarantined")


@pytest.mark.parametrize("final", [False, True], ids=["automatic", "wake-me"])
def test_background_wait_outliving_the_result_does_not_swallow_the_wake(e2e, final):
    release = e2e.root / "release-run"
    e2e.env["SUBFLEET_FAKE_RELEASE_PATH"] = str(release)          # the run waits for this file
    e2e.env["SUBFLEET_FAKE_TURN_LOG"] = str(e2e.root / "turns.jsonl")
    e2e.env["SUBFLEET_FAKE_BASH_COMMAND"] = shlex.join(
        [sys.executable, "-m", "subfleet.cli", *e2e.run_args("astra", "--name", "strand-run")])
    e2e.env["SUBFLEET_FAKE_BG_COMMAND"] = shlex.join([sys.executable, "-m", "subfleet.cli", "wait"])
    if final:
        e2e.env["SUBFLEET_FAKE_BASH_WAKE"] = "1"                     # also ends with WAKE-ME: runs=<id>
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
    release.write_text("go")                                       # the run finishes after the turn's result
    assert conv.until_state(mid, "complete", "failed", "delivery-unknown", timeout=90)["state"] == "complete"
    background = e2e.until(lambda: [r for r in conv.turn_log() if "background" in r], timeout=90)
    assert background[0]["rc"] == 0, background
    e2e.until(lambda: e2e.job(job_id)["state"] in TERMINAL, timeout=60)
    e2e.until(lambda: all(j["state"] in TERMINAL for j in e2e.rows("SELECT state FROM jobs WHERE kind='turn'")),
              timeout=60)
    deadline, wakes = time.monotonic() + 20, []
    while time.monotonic() < deadline and not wakes:
        wakes = [m for m in conv.call("conversation.open", conversation_id=cid)["messages"] if m["origin"] == "wake"]
        time.sleep(0.25)
    notices = e2e.rows("SELECT state,transport,acknowledged_at FROM notices WHERE job_id=?", (job_id,))
    with sqlite3.connect(f"file:{e2e.root / 'conversations.sqlite3'}?mode=ro", uri=True) as db:
        has = db.execute("SELECT 1 FROM sqlite_master WHERE name='wake_requests'").fetchone()
        requests = db.execute("SELECT kind,state FROM wake_requests WHERE conversation_id=?", (cid,)).fetchall() \
            if has else "(no table)"
    print(f"R2 strand ({'wake-me' if final else 'automatic'}): wakes={len(wakes)} notice={notices} "
          f"requests={requests} waiter_stdout={background[0]['stdout'].strip()[:120]!r}")
    assert wakes, (f"run {job_id} finished after the turn's result and the conversation was never woken; "
                   f"notice {notices}, wake requests {requests}")
