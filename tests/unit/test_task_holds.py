"""C-11.10 (D-TASKHOLD1): the task hold's pure reader and validators, and its CLI.

Properties, for every input drawn: a note is kept only when it holds no control
character anywhere (edges included) and is one printable line of at most 160
characters with no credential in it, and then kept as given less its surrounding
ASCII spaces; the release hint gives any task back whole to a shell; an end is
kept only when it is a later instant, to
the whole second; a row is in force exactly while the clock is before its end;
and `in_force` reads holds only from rows of its own key.
"""

from __future__ import annotations

import json
import shlex
import unicodedata
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings, strategies as st

from subfleet import cli, protocol, refusals, render, task_holds
from subfleet.contracts import Exit
from tests.disk_floor_model import stamp
from tests.task_hold_support import fresh_daemon, hold

NOW = "2026-10-24T12:00:00Z"
#: What no note may hold anywhere (review r1, P3-2): every C0 and C1 control and DEL (Unicode's
#: `Cc`), and the line and paragraph separators. The test's own list, read from Unicode.
CONTROLS = frozenset({chr(n) for n in range(0x110000) if unicodedata.category(chr(n)) == "Cc"}
                     | {"\u2028", "\u2029"})
SETTINGS = settings(max_examples=300, deadline=None, derandomize=True, database=None)


# --- the note ------------------------------------------------------------------------------------

@SETTINGS
@given(st.text(max_size=200))
def test_a_note_is_kept_exactly_when_it_is_one_printable_line_within_the_limit(text):
    stripped = text.strip(" ")
    ok = (not any(char in CONTROLS for char in text) and bool(stripped)
          and len(stripped) <= task_holds.NOTE_MAX and stripped.isprintable())
    try:
        kept = task_holds.note_text(text)
    except ValueError:
        assert not ok or task_holds.ids.holds_credential(stripped)
    else:
        assert ok and kept == stripped


@SETTINGS
@given(st.text(max_size=40), st.sampled_from(sorted(CONTROLS)),
       st.sampled_from(["start", "end", "both", "inside"]))
def test_a_control_anywhere_is_refused_before_anything_is_trimmed(text, control, where):
    """Review r1, P3-2: `"\\nbooking\\n"` was kept as `booking`, its edges stripped first."""
    note = {"start": control + text, "end": text + control, "both": control + text + control,
            "inside": text[:len(text) // 2] + control + text[len(text) // 2:]}[where]
    with pytest.raises(ValueError):
        task_holds.note_text(note)


def test_the_controls_are_every_c0_c1_and_del_and_the_line_and_paragraph_separators():
    assert task_holds.CONTROLS == CONTROLS


@pytest.mark.parametrize("value", [None, 0, ["a"], "", "  ", "a\nb", "a\rb", "a\tb", "\x00", "x" * 161,
                                   "token sk-ant-oat01-" + "Q" * 40,
                                   "\nbooking\n", "\tbooking\t", "booking\n", "\rbooking", "\x7fbooking",
                                   "booking\x85", "\u2028booking", "booking\u2029", "\x1bbooking", "\u3000booking"])
def test_a_note_that_is_not_one_printable_line_is_refused(value):
    with pytest.raises(ValueError):
        task_holds.note_text(value)


def test_a_note_of_160_characters_and_unicode_is_kept():
    assert task_holds.note_text("é" * 160) == "é" * 160
    assert task_holds.note_text(" uk-data: batch 7 (Fleet ops) ") == "uk-data: batch 7 (Fleet ops)"


# --- the end -------------------------------------------------------------------------------------

@SETTINGS
@given(st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2200, 1, 1),
                    timezones=st.sampled_from([timezone.utc, timezone(timedelta(hours=-4)), None])))
def test_an_end_is_kept_to_the_second_exactly_when_it_is_later_than_now(when):
    text = when.isoformat()
    aware = when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when
    whole = aware.astimezone(timezone.utc).replace(microsecond=0)
    try:
        kept = task_holds.until_text(text, NOW)
    except ValueError:
        assert whole <= task_holds.instant(NOW)
    else:
        assert whole > task_holds.instant(NOW)
        assert kept == whole.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize("value", [None, "", "  ", "tomorrow", "2026-13-01T00:00:00Z", "2026-10-24T25:00",
                                   NOW, "2026-10-24T11:59:59Z", "2026-10-24T12:00:00.900Z", 1761307200])
def test_an_end_that_is_not_a_later_instant_is_refused(value):
    with pytest.raises(ValueError):
        task_holds.until_text(value, NOW)


def test_an_end_with_no_zone_is_utc_and_an_offset_is_converted():
    assert task_holds.until_text("2026-10-24T13:00:00", NOW) == "2026-10-24T13:00:00Z"
    assert task_holds.until_text("2026-10-24T09:30:00-04:00", NOW) == "2026-10-24T13:30:00Z"


# --- reading rows --------------------------------------------------------------------------------

ROWS = st.lists(st.fixed_dictionaries({
    "lease_key": st.one_of(st.sampled_from(["task-hold:build", "task-hold:review", "task-hold:", "lane:codex-1:slot:0",
                                            "out:/x", "worktree:/w", "task-holds:build"]),
                           st.text(max_size=20)),
    "holder": st.one_of(st.sampled_from(["operator: booking", "operator: ", "20261024-000000-build", "probe:x"]),
                        st.text(max_size=20)),
    "acquired_at": st.just("2026-10-24T11:00:00Z"),
    "expires_at": st.one_of(st.none(), st.sampled_from(["2026-10-24T11:59:59Z", NOW, "2026-10-24T12:00:01Z",
                                                        "2027-01-01T00:00:00Z"])),
}), max_size=6)


@SETTINGS
@given(ROWS)
def test_only_task_hold_rows_before_their_end_are_in_force(rows):
    holds = task_holds.in_force(rows, NOW)
    for task, hold in holds.items():
        source = [row for row in rows if row["lease_key"] == "task-hold:" + task]
        assert source and task
        assert hold["until"] > NOW
        assert hold["until"] == source[-1]["expires_at"] or any(row["expires_at"] == hold["until"] for row in source)
    for row in rows:
        key = row["lease_key"]
        if (isinstance(key, str) and key.startswith("task-hold:") and len(key) > len("task-hold:")
                and isinstance(row["expires_at"], str) and row["expires_at"] > NOW):
            assert key[len("task-hold:"):] in holds


def test_a_row_is_spelled_and_read_back():
    row = {"lease_key": task_holds.lease_key("build"), "holder": task_holds.HOLDER + "uk-data",
           "acquired_at": "2026-10-24T11:00:00Z", "expires_at": "2026-10-24T13:00:00Z"}
    assert task_holds.in_force([row], NOW) == {"build": {"task": "build", "until": "2026-10-24T13:00:00Z",
                                                         "note": "uk-data", "since": "2026-10-24T11:00:00Z"}}
    assert task_holds.listing(task_holds.in_force([row], NOW)) == [
        {"task": "build", "until": "2026-10-24T13:00:00Z", "note": "uk-data", "since": "2026-10-24T11:00:00Z"}]
    assert task_holds.wait_hold(task_holds.in_force([row], NOW)["build"]) == {
        "reason": "task-held", "task": "build", "until": "2026-10-24T13:00:00Z", "note": "uk-data",
        "next_check_at": "2026-10-24T13:00:00Z"}


def test_the_key_range_covers_exactly_the_task_hold_keys():
    low, high = task_holds.ROWS_PARAMS
    for key in ("task-hold:build", "task-hold:authored-prose", "task-hold:zz"):
        assert low <= key < high
    for key in ("task-hold", "task-holds:build", "task-hold;", "lane:codex-1:slot:0", "worktree:/x", "out:/x",
                "session:abc:revive", "retire:job", "conversation:c", "native:codex:s"):
        assert not (low <= key < high), key


def test_no_reaper_holder_or_job_id_looks_like_a_hold_holder():
    for holder in ("probe:timer:x", "retention:20261024-000000-x", "resume:x", "gate-round:x",
                   "20261024-000000-build", "20261024-000000-build-a1"):
        assert not holder.startswith(task_holds.HOLDER)


# --- refusals render their own words ---------------------------------------------------------------

def test_the_refusals_read_their_templates():
    assert refusals.render("unknown-task", {"task": "builds"}) == (
        "tasks: unknown task builds", refusals.HANDLER_MESSAGES["unknown-task"][1])
    assert refusals.render("unknown-task", {"task": "sk-ant-oat01-" + "Q" * 40})[0] == "tasks: unknown task"
    assert refusals.render("task-hold-note", {})[0].startswith("tasks hold: --note is required")
    assert refusals.render("task-hold-until", {})[0] == "tasks hold: --until must be a later ISO 8601 instant"
    assert refusals.render("tasks-action", {})[0] == "tasks: the action is list, hold or release"


def test_the_why_text_names_the_note_the_end_and_the_release():
    lines = render.why_queue({"job_id": "20261024-000000-build", "kind": "dispatch", "state": "waiting",
                              "wait_reason": "task-held", "next_check_at": "2026-10-24T13:00:00Z",
                              "hold": {"reason": "task-held", "task": "build", "until": "2026-10-24T13:00:00Z",
                                       "note": "uk-data batch", "next_check_at": "2026-10-24T13:00:00Z"}})
    assert lines[1] == ("Held: the operator holds build jobs at admission until 2026-10-24T13:00:00Z "
                        "(uk-data batch); it starts once the hold ends or `subfleet tasks release -- build`, "
                        "and holds no other job back (C-11.10)")


def why_command(task: str) -> str:
    """The release command a `task-held` job's `why` line names, from between its backticks."""
    lines = render.why_queue({"job_id": "20261024-000000-x", "kind": "dispatch", "state": "waiting",
                              "wait_reason": "task-held", "hold": {"reason": "task-held", "task": task,
                                                                    "until": "2026-10-24T13:00:00Z", "note": "n"}})
    return lines[1].split("`")[1]


@pytest.mark.parametrize("task,command", [
    ("build", "subfleet tasks release -- build"),
    ("data build", "subfleet tasks release -- 'data build'"),
    ("-x", "subfleet tasks release -- -x"),
    ("it's", "subfleet tasks release -- 'it'\"'\"'s'"),
    ("$(touch x)", "subfleet tasks release -- '$(touch x)'"),
])
def test_the_release_hint_quotes_the_task_as_one_argument_after_a_double_dash(task, command):
    """Review r1, P3-3: `data build` rendered as `subfleet tasks release data build`, two arguments."""
    assert task_holds.release_command(task) == command
    assert why_command(task) == command
    argv = shlex.split(command)
    assert argv == ["subfleet", "tasks", "release", "--", task]
    parsed = cli.build_parser().parse_args(argv[1:])
    assert (parsed.handler.__name__, parsed.tasks_command, parsed.task) == ("cmd_tasks", "release", task)


@SETTINGS
@given(st.text(min_size=1, max_size=60))
def test_any_task_comes_back_whole_from_its_release_hint(task):
    assert shlex.split(task_holds.release_command(task)) == ["subfleet", "tasks", "release", "--", task]


# --- the CLI -------------------------------------------------------------------------------------

def test_tasks_verbs_parse_to_their_handler():
    parser = cli.build_parser()
    for argv in (["tasks"], ["tasks", "list"], ["tasks", "release", "build"],
                 ["tasks", "hold", "build", "--until", "2026-10-25T00:00:00Z", "--note", "uk-data"]):
        assert parser.parse_args(argv).handler.__name__ == "cmd_tasks"


def test_tasks_hold_and_release_reach_the_tasks_op(daemon, capsys):
    holds = []

    def answer(request):
        action = request.args.get("action")
        if action == "hold":
            hold = {"task": request.args["task"], "until": request.args["until"], "note": request.args["note"],
                    "since": NOW}
            holds[:] = [hold]
            return {"held": hold["task"], "task_hold": hold, "replaced": None, "woken": 0, "task_holds": holds}
        if action == "release":
            holds.clear()
            return {"released": request.args["task"], "was_held": True, "woken": 2, "task_holds": []}
        return {"task_holds": holds}
    server = daemon({"tasks": answer})
    assert cli.main(["tasks", "hold", "build", "--until", "2026-10-25T00:00:00Z", "--note", "uk-data batch"]) == 0
    assert server.args("tasks") == {"action": "hold", "task": "build", "until": "2026-10-25T00:00:00Z",
                                    "note": "uk-data batch"}
    out = capsys.readouterr().out
    assert "held: build until 2026-10-25T00:00:00Z (uk-data batch)" in out
    assert cli.main(["tasks"]) == 0
    assert "build" in capsys.readouterr().out
    assert cli.main(["tasks", "release", "build", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["woken"] == 2
    assert server.args("tasks") == {"action": "release", "task": "build", "until": None, "note": None}


@pytest.mark.parametrize("argv,words", [
    (["tasks", "hold", "build", "--until", "2026-10-25T00:00:00Z"], "--note is required"),
    (["tasks", "hold", "build", "--until", "2026-10-25T00:00:00Z", "--note", "a\nb"], "one line"),
    (["tasks", "hold", "build", "--until", "2026-10-25T00:00:00Z", "--note", "x" * 161], "at most 160"),
    (["tasks", "hold", "build", "--until", "2026-10-25T00:00:00Z", "--note", "\nbooking\n"], "one line"),
    (["tasks", "hold", "build", "--until", "2026-10-25T00:00:00Z", "--note", "\tbooking\t"], "one line"),
    (["tasks", "hold", "build", "--until", "2026-10-25T00:00:00Z", "--note", "booking\u2028"], "one line"),
    (["tasks", "hold", "build", "--until", "2026-10-25T00:00:00Z", "--note", "\n"], "one line"),
    (["tasks", "hold", "build", "--until", "soon", "--note", "uk-data"], "ISO 8601"),
])
def test_tasks_hold_refuses_a_bad_note_or_end_before_sending_anything(daemon, capsys, argv, words):
    server = daemon({"tasks": lambda request: {"task_holds": []}})
    assert cli.main(argv) == int(Exit.INVALID_INPUT)
    assert words in capsys.readouterr().err
    assert server.ops() == []


def test_an_older_daemon_is_named_as_older(daemon, capsys):
    daemon({})                                            # answers every op `unknown op`
    assert cli.main(["tasks", "hold", "build", "--until", "2026-10-25T00:00:00Z", "--note", "uk-data"]) \
        == int(Exit.DAEMON_UNAVAILABLE)
    err = capsys.readouterr().err
    assert "older than this CLI" in err


def test_status_text_lists_the_holds_with_their_waiting_jobs():
    data = {"lanes": [], "readings": [], "closures": [],
            "task_holds": [{"task": "build", "until": "2026-10-25T00:00:00Z", "note": "uk-data batch",
                            "since": NOW}],
            "jobs": [{"job_id": "a", "kind": "dispatch", "task": "build", "state": "waiting",
                      "wait_reason": "task-held"},
                     {"job_id": "b", "kind": "dispatch", "task": "review", "state": "running"}]}
    text = cli.format_status(data)
    lines = text.splitlines()
    start = lines.index("task holds")
    assert lines[start + 1].split() == ["build", "until", "2026-10-25T00:00:00Z", "(1", "waiting):",
                                        "uk-data", "batch"]
    assert "task holds" not in cli.format_status({**data, "task_holds": []})
    assert "task holds" not in cli.format_status({k: v for k, v in data.items() if k != "task_holds"})


def test_readings_status_lists_the_task_hold_in_force():
    """Review r6, R6-3: readings must keep the task hold's task, end and note."""
    with fresh_daemon() as (service, _, _, _):
        until = stamp(3600)
        note = "uk-data batch release"
        hold(service, "build", until, note=note)
        assert f"Task holds: build until {until}: {note}" in service.dispatch("readings", {})["status"]


def test_status_text_counts_a_holds_jobs_from_its_own_total_not_the_rows_listed():
    """Review r1, P3-1: offline status lists 50 jobs, and 51 held builds read `(50 waiting)`."""
    data = {"lanes": [], "readings": [], "closures": [],
            "task_holds": [{"task": "build", "until": "2026-10-25T00:00:00Z", "note": "uk-data batch",
                            "since": NOW, "waiting": 51}],
            "jobs": [{"job_id": "a", "kind": "dispatch", "task": "build", "state": "waiting",
                      "wait_reason": "task-held"}]}
    assert "(51 waiting)" in cli.format_status(data)
    assert "(0 waiting)" in cli.format_status({**data, "task_holds": [{**data["task_holds"][0], "waiting": 0}]})


def test_the_listing_carries_the_count_only_when_given_one():
    holds = {"build": {"task": "build", "until": "2026-10-25T00:00:00Z", "note": "n", "since": NOW}}
    assert task_holds.listing(holds) == [{"task": "build", "until": "2026-10-25T00:00:00Z", "note": "n",
                                          "since": NOW}]
    assert task_holds.listing(holds, waiting={"build": 51, "review": 3})[0]["waiting"] == 51
    assert task_holds.listing(holds, waiting={})[0]["waiting"] == 0
    assert task_holds.waiting_counts([{"task": "build", "n": 51}, {"task": None, "n": 2}]) == {"build": 51}


def test_the_protocol_names_the_op():
    assert "tasks" in protocol.OPS
    assert protocol.coerce_args(protocol.TasksArgs, {"action": "hold", "task": "build", "x": 1}).task == "build"
