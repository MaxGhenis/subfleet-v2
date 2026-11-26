"""The launch baseline and the app's watch loop, through a real UIModel."""

from pathlib import Path
import tempfile

import pytest

from tests.frontend.conftest import needs_swift, run_probe, write_json
from tests.frontend.daemon_harness import ServiceHarness, claude_assistant, claude_init, claude_result
from tests.frontend.swift import ROOT, compile_probe

pytestmark = needs_swift


@pytest.fixture(scope="module")
def connect_probe(tmp_path_factory):
    return compile_probe(tmp_path_factory.mktemp("subfleet-connect-baseline") / "probe",
                         [ROOT / "tests/frontend/ConnectBaselineProbe.swift"], "SUBFLEET_UI_MODEL_TEST")


@pytest.fixture
def harness():
    # Keep the service's socket paths short and remove the root after closing it.
    with tempfile.TemporaryDirectory(prefix="sf-cb-", dir="/tmp") as root:
        harness = ServiceHarness(Path(root))
        try:
            yield harness
        finally:
            harness.close()


def finish_turn(harness, cid):
    mid = harness.submit(cid, "run")["message_id"]
    harness.attempt(cid, mid).feed(
        claude_init(),
        {"type": "user", "uuid": mid, "isReplay": True, "message": {"role": "user", "content": "x"}},
        claude_assistant("m1", [{"type": "text", "text": "Done."}]),
        claude_result(),
    )
    return mid


def answers_for(harness, cid, mid):
    return {
        "capabilities": harness.call("capabilities"),
        "models.list": harness.call("models.list", provider="claude"),
        "conversation.list": harness.call("conversation.list"),
        "conversation.status": harness.call("conversation.status", conversation_id=cid),
        "message.status": harness.call("message.status", message_ids=[mid]),
        "conversation.open": harness.call("conversation.open", conversation_id=cid),
    }


@pytest.fixture
def finished_turn(harness):
    cid = harness.create(title="Elsewhere")["conversation_id"]
    mid = finish_turn(harness, cid)
    page = harness.call("conversation.watch", after=0)
    return mid, page, answers_for(harness, cid, mid)


def empty_page(cursor):
    return {"changes": [], "next": cursor}


def connect(probe, tmp_path, *, answers, drain, **scenario):
    root = tmp_path / "model"
    input_path = write_json(tmp_path / "connect.json", {
        "root": str(root), "focused": None, "on_screen": False,
        "answers": answers, "drain": drain, **scenario,
    })
    return run_probe(probe, input_path, env={"SUBFLEET_HOME": str(root)})


def assert_result(result, **expected):
    actual = {key: result[key] for key in expected}
    assert actual == expected, actual


def test_connect_keeps_what_reaches_the_state_during_the_baseline(connect_probe, tmp_path, finished_turn):
    _, page, answers = finished_turn
    end = page["next"]
    result = connect(connect_probe, tmp_path, answers=answers,
                     drain={"0": [page], str(end): [empty_page(end)]},
                     during_first_drain={"search": "probe", "grouping": "workspace"})
    assert_result(result, search="probe", grouping="workspace", watch_baselined=True,
                  watch_cursor=end, drain_reads=[0, end])


def test_a_failed_baseline_read_is_read_again_before_the_feed_notifies(connect_probe, tmp_path, finished_turn):
    mid, page, answers = finished_turn
    rows, end = page["changes"], page["next"]
    done = next(i for i, row in enumerate(rows) if row["state"] == "complete" and row["message_id"] == mid)
    cursor = rows[done - 1]["seq"]
    page1 = {"changes": rows[:done], "next": cursor}
    page2 = {"changes": rows[done:], "next": end}
    result = connect(connect_probe, tmp_path, answers=answers, drain={
        "0": [page1], str(cursor): [{"error": "watch read failed"}, page2], str(end): [empty_page(end)],
    }, after_connect=page2)
    assert_result(result, drain_reads=[0, cursor, cursor, end], watch_baselined=True,
                  watch_cursor=end, backlog=[], problem="watch read failed")


def test_a_baseline_longer_than_64_pages_is_read_to_its_end(connect_probe, tmp_path, finished_turn):
    mid, page, answers = finished_turn
    turn_rows = {row["state"]: row for row in page["changes"] if row["message_id"] == mid}
    live_row = turn_rows.get("running", turn_rows["queued"])
    last_page = {"changes": [{**turn_rows["complete"], "seq": 65}], "next": 65}
    drain = {
        str(seq - 1): [{"changes": [{**live_row, "seq": seq}], "next": seq}]
        for seq in range(1, 65)
    }
    drain.update({"64": [last_page], "65": [empty_page(65)]})
    result = connect(connect_probe, tmp_path, answers=answers, drain=drain, after_connect=last_page)
    assert_result(result, drain_reads=list(range(66)), watch_cursor=65, watch_baselined=True, backlog=[])


@pytest.mark.parametrize("flip_on_screen", [True, False], ids=["on", "off"])
def test_the_feed_reads_the_screen_as_each_page_lands(connect_probe, tmp_path, harness, flip_on_screen):
    cid = harness.create(title="Focused")["conversation_id"]
    base = harness.call("conversation.watch", after=0)
    mid = finish_turn(harness, cid)
    feed = harness.call("conversation.watch", after=base["next"])
    drain = {"0": [base]}
    if base["changes"]:
        drain[str(base["next"])] = [empty_page(base["next"])]
    result = connect(connect_probe, tmp_path, answers=answers_for(harness, cid, mid), drain=drain,
                     focused=cid, on_screen=not flip_on_screen, feed=feed, flip_on_screen=flip_on_screen)
    assert_result(result, posted=[] if flip_on_screen else [["completed", cid]], feed_reads=2)


def test_probe_seams_exist_only_in_ui_model_test_builds():
    seams = ("onScreenOverride", "probePosted")
    found = set()
    outside = []
    for source in sorted((ROOT / "app/Sources").glob("*.swift")):
        branches = []
        for number, raw_line in enumerate(source.read_text().splitlines(), 1):
            line = raw_line.strip()
            directive = line.split(maxsplit=1)[0] if line else ""
            if directive == "#if":
                branches.append(line)
            elif directive in {"#elseif", "#else"}:
                branches[-1] = line
            elif directive == "#endif":
                branches.pop()
            if line.startswith("//"):
                continue
            for seam in seams:
                if seam in line:
                    found.add(seam)
                    if branches[-1:] != ["#if SUBFLEET_UI_MODEL_TEST"]:
                        outside.append(f"{source.name}:{number}: {line}")
    assert (outside, set(seams) - found) == ([], set())
