"""Crash/relaunch histories over the real UI outbox and real conversation service.

The UI probe is a foreground child; the isolated service runs on the existing
in-process socket harness. Crashes use _exit after the real service has accepted
create/submit, or terminate the UI between user actions. No live daemon is used.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

from hypothesis import example, given, seed, settings, strategies as st

from tests.frontend.conftest import needs_swift, write_json
from tests.frontend.daemon_harness import ServiceHarness, ServiceServer
from tests.frontend.test_app_cutover_start import refused_journal
from tests.frontend.test_pr124_fixes_ui import data_for, review_probe, save_composer  # noqa: F401

pytestmark = needs_swift

history = st.lists(st.tuples(st.sampled_from(["retry", "discard", "crash", "relaunch", "open", "send-composer"]),
                            st.integers(0, 1), st.sampled_from([None, "conversation.create", "message.submit"])),
                   min_size=1, max_size=8)


def invoke(probe, folder, data):
    source = write_json(folder / "history.json", data)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SUBFLEET_")}
    result = subprocess.run([str(probe), str(source)], capture_output=True, text=True, env=env, timeout=30)
    assert result.returncode in (0, 73), (result.returncode, result.stderr)
    return None if result.returncode == 73 else json.loads(result.stdout)["snapshots"][-1]


@seed(int(os.environ.get("PR124_PROPERTY_SEED", "124")))
@settings(max_examples=10, deadline=None, database=None)
@example([("retry", 1, None), ("open", 0, None), ("send-composer", 0, None), ("relaunch", 0, None)])
@example([("retry", 1, "conversation.create"), ("relaunch", 0, None), ("retry", 1, "message.submit"),
          ("crash", 0, None), ("relaunch", 0, None)])
@example([("discard", 0, None), ("retry", 1, "message.submit"), ("relaunch", 0, None)])
@given(history)
def test_every_written_message_is_sent_once_or_remains_visibly_unsent(review_probe, history):
    print("RECOVERY_HISTORY " + json.dumps(history), flush=True)
    with tempfile.TemporaryDirectory(prefix="pr124-history-", dir="/private/tmp") as raw:
        folder = Path(raw)
        world = ServiceHarness(folder / "daemon")
        server = ServiceServer(world)
        try:
            data = data_for(folder, world)
            data["socket"] = str(server.path)
            save_composer(data, world)
            journal_path = Path(data["root"]) / "support/outbox.json"
            mid, journal = refused_journal(journal_path, world.settings(permission="read-only"))
            mid0 = str(uuid.uuid4())
            message = dict(journal["entries"][-1], key=mid0, order=19, conversation="draft:app-first")
            message["message"] = {**message["message"], "text": "first written message"}
            journal["entries"].append(message)
            journal["nextOrder"] = 20
            write_json(journal_path, journal)
            originals = {"app-first": mid0, "app-second": mid}
            discarded = set()
            expected_texts = {mid0: "first written message", mid: journal["entries"][2]["message"]["text"]}

            for action, index, crash_op in history:
                key = ["app-first", "app-second"][index]
                entries = json.loads(journal_path.read_text())["entries"]
                failed = any(e["key"] == key and e["state"] == "failed" for e in entries)
                steps = []
                if action == "retry" and failed:
                    steps = [{"action": "change-failure", "id": key},
                             {"action": "folder", "path": str(world.workspace)}, {"action": "start"}]
                elif action == "discard" and failed:
                    steps = [{"action": "discard", "id": key}]
                    discarded.add(originals[key])
                elif action == "open":
                    # Leaving retry editing must show the ordinary saved composer.
                    if failed:
                        steps.append({"action": "change-failure", "id": key})
                    steps.append({"action": "open"})
                elif action == "send-composer":
                    steps = [{"action": "open"}, {"action": "start"}]
                elif action == "crash":
                    steps = [{"action": "crash"}]
                else:
                    steps = [{"action": "relaunch-pump"}]
                shot = invoke(review_probe, folder, {**data, "steps": steps, "crash_after": crash_op})
                # Relaunch is real: a fresh process reopens the same journal after
                # every segment, including an unanswered accepted operation.
                shot = invoke(review_probe, folder, {**data, "steps": [{"action": "relaunch-pump"}]})
                assert shot is not None
                entries = json.loads(journal_path.read_text())["entries"]
                submitted = [{"message_id": row["message_id"],
                              "text": world.store.message_text(world.store.message(row["message_id"]))}
                             for row in world.store.query("SELECT * FROM messages WHERE origin='person'")]
                by_text = {}
                for row in submitted:
                    by_text.setdefault(row["text"], []).append(row["message_id"])
                assert all(len(ids) <= 1 for ids in by_text.values()), (history, submitted)
                for original, text in expected_texts.items():
                    matching = [e for e in entries if e.get("message", {}).get("text") == text]
                    assert len(matching) == 1 and matching[0]["key"] == original
                    if original in discarded:
                        assert matching[0]["state"] == "withdrawn"
                    elif not by_text.get(text):
                        # Unaccepted messages must be visible on their refused
                        # row after relaunch, not merely present somewhere on disk.
                        assert matching[0]["state"] in ("queued", "failed")
                        assert any(d["id"] == matching[0]["conversation"].removeprefix("draft:")
                                   and original in d["messages"] for d in shot["failed"])
                idea = [e for e in entries if e.get("message", {}).get("text") == "my unsent idea"]
                assert len(idea) <= 1
                assert idea or shot["saved_text"] == "my unsent idea"
                # Even a crash after submit cannot change the original message id.
                assert all(by_text.get(text, [mid]) == [mid] for mid, text in expected_texts.items()
                           if mid not in discarded)
        finally:
            server.close()
            server._thread.join(timeout=5)
            world.close()
