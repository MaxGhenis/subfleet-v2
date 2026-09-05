"""The three Claude Code hook entry points (C-15.2, C-15.3).

Every test names the clause it proves (C-20.5). The exit codes asserted here
are the harness's, not C-17.3's — `docs/reference/claude-hooks.md` section 3 is
the source for which event may exit 2 and what that exit means.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

from subfleet import hooks
from subfleet.client import Client
from subfleet.contracts import Exit

JOB = "20260905-120000-demo"
OTHER = "20260905-120001-other"
SESSION = "sess-hook"


def payload(event: str, **extra) -> dict:
    return {"session_id": SESSION, "hook_event_name": event, "cwd": "/repo",
            "transcript_path": "/dev/null", **extra}


def notice(notice_id: int, job_id: str = JOB, state: str = "pending",
           text: str = "run finished") -> dict:
    return {"notice_id": notice_id, "session_id": SESSION, "job_id": job_id,
            "state": state, "text": text}


# --- SessionStart and UserPromptSubmit (layer 3) ------------------------------

@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
def test_session_events_surface_pending_notices_and_mark_them(daemon, root, event):
    """C-15.2 layer 3 surfaces `pending`/`offered` rows; C-15.3 marks `surfaced`."""
    marked: list[dict] = []
    server = daemon({
        "notice.pending": lambda request: {"notices": [notice(1), notice(2, OTHER)]},
        "notice.mark": lambda request: marked.append(request.args) or {"notices": []},
    })
    stdout = io.StringIO()
    assert hooks.session_event(event, payload(event), root, stdout=stdout) == 0

    emitted = json.loads(stdout.getvalue())
    context = emitted["hookSpecificOutput"]["additionalContext"]
    assert emitted["hookSpecificOutput"]["hookEventName"] == event
    assert "2 detached runs" in context and "run finished" in context
    assert marked == [{"session_id": SESSION, "notice_ids": [1, 2],
                       "state": "surfaced", "transport": f"hook:{event}"}]
    assert "notice.pending" in server.ops()


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
def test_session_events_exit_zero_with_nothing_to_say(daemon, root, event):
    """C-15.2 exit 2 on either event is destructive, so silence is exit 0.

    `docs/reference/claude-hooks.md` §3: exit 2 on SessionStart blocks session
    startup and on UserPromptSubmit blocks the prompt AND ERASES IT.
    """
    daemon({"notice.pending": lambda request: {"notices": []}})
    stdout = io.StringIO()
    assert hooks.session_event(event, payload(event), root, stdout=stdout) == 0
    assert stdout.getvalue() == ""


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
def test_session_events_exit_zero_when_the_daemon_is_gone(root, event):
    """C-15.2 a hook that fails loudly on a dead daemon is worse than a late notice."""
    stdout = io.StringIO()
    assert hooks.session_event(event, payload(event), root, stdout=stdout) == 0
    assert stdout.getvalue() == ""


def test_session_event_without_a_session_id_does_nothing(daemon, root, monkeypatch):
    """C-15.1 notices are keyed by caller session; no session, no rows to surface."""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    server = daemon({"notice.pending": lambda request: {"notices": [notice(1)]}})
    stdout = io.StringIO()
    assert hooks.session_event("SessionStart", {"hook_event_name": "SessionStart"},
                               root, stdout=stdout) == 0
    assert stdout.getvalue() == "" and server.ops() == []


def test_session_event_falls_back_to_the_session_env(daemon, root, monkeypatch):
    """C-17.6 the harness exports CLAUDE_CODE_SESSION_ID to every hook process."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    daemon({"notice.pending": lambda request: {"notices": [notice(1)]},
            "notice.mark": lambda request: {"notices": []}})
    stdout = io.StringIO()
    assert hooks.session_event("SessionStart", {"hook_event_name": "SessionStart"},
                               root, stdout=stdout) == 0
    assert "run finished" in stdout.getvalue()


def test_render_pending_keeps_v1s_shape(root):
    """C-15.2 the surfaced text does not change across the cutover.

    v1 `notify.render_pending` builds a header, one block per notice, and a
    footer, joined by blank lines. Compared against that function's literal
    format strings, not against a remembered example.
    """
    text = hooks.render_pending([notice(1, text="a"), notice(2, OTHER, text="b")])
    assert text == (
        "subfleet: 2 detached runs dispatched by this session finished while it "
        "was not running:\n\na\n\nb\n\n"
        "List: subfleet runs --mine · details: subfleet runs show <id>")
    assert "1 detached run dispatched" in hooks.render_pending([notice(1)])
    assert hooks.render_pending([]) == ""


def test_render_pending_names_the_job_when_a_row_has_no_text(root):
    """C-15.1 a notice row always carries text; a row without it still names its job."""
    assert "run 20260905-120000-demo finished" in hooks.render_pending(
        [{"notice_id": 1, "job_id": JOB, "text": ""}])


# --- PostToolUse (layer 2) ----------------------------------------------------

class Clock:
    """A monotonic clock the test advances, so no test spends real seconds."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)


def running_job(job_id: str = JOB, request_id: str = "req-1") -> dict:
    return {"job_id": job_id, "request_id": request_id, "state": "running",
            "rc": None, "out_path": None}


def finished_job(job_id: str = JOB) -> dict:
    return {"job_id": job_id, "state": "succeeded", "rc": 0,
            "out_path": "/repo/out.md"}


def test_post_tool_use_delivers_a_finished_job_with_exit_two(daemon, root):
    """C-15.2 layer 2: exit 2 with the notice on stderr is how a PostToolUse hook
    shows anything at all (`claude-hooks.md` §1, the asyncRewake limitation)."""
    marked: list[dict] = []
    daemon({
        "list": lambda request: {"jobs": [running_job()]},
        "wait": lambda request: {"jobs": [finished_job()]},
        "notice.pending": lambda request: {"notices": [notice(7, text="demo done")]},
        "notice.mark": lambda request: marked.append(request.args) or {"notices": []},
    })
    stderr = io.StringIO()
    clock = Clock()
    code = hooks.post_tool_use(
        payload("PostToolUse", tool_name="Bash",
                tool_input={"command": "subfleet run -p p.md"},
                tool_response=f"{JOB}\nrequest=req-1"),
        root, budget_s=30, stderr=stderr, now=clock, sleep=clock.sleep)
    assert code == int(Exit.INVALID_INPUT) == 2
    assert stderr.getvalue().strip() == "demo done"
    # `offered`, not `surfaced`: this transport gets no acknowledgement, so
    # layer 3 must be free to show the row again (C-15.3).
    assert marked == [{"session_id": SESSION, "notice_ids": [7],
                       "state": "offered", "transport": "hook:PostToolUse"}]


def test_post_tool_use_exits_zero_and_silent_on_timeout(daemon, root):
    """C-15.2 a hook with nothing to say says nothing; layer 3 catches up later."""
    daemon({"list": lambda request: {"jobs": [running_job()]},
            "wait": lambda request: {"timeout": True}})
    stderr = io.StringIO()
    clock = Clock()
    code = hooks.post_tool_use(
        payload("PostToolUse", tool_name="Bash",
                tool_input={"command": "subfleet run -p p.md"}, tool_response=JOB),
        root, budget_s=2, stderr=stderr, now=clock, sleep=clock.sleep)
    assert code == int(Exit.OK) == 0
    assert stderr.getvalue() == ""


def test_post_tool_use_reports_a_job_with_no_notice_row_from_the_job_itself(
        daemon, root):
    """C-15.1 nothing is invented: every field of the fallback line is copied."""
    daemon({"list": lambda request: {"jobs": [running_job()]},
            "wait": lambda request: {"jobs": [finished_job()]},
            "notice.pending": lambda request: {"notices": []}})
    stderr = io.StringIO()
    clock = Clock()
    assert hooks.post_tool_use(
        payload("PostToolUse", tool_name="Bash",
                tool_input={"command": "subfleet run -p p.md"}, tool_response=JOB),
        root, budget_s=30, stderr=stderr, now=clock, sleep=clock.sleep) == 2
    text = stderr.getvalue()
    assert JOB in text and "succeeded" in text and "rc=0" in text
    assert "/repo/out.md" in text and "subfleet runs show" in text


def test_the_lease_stops_a_second_hook_waiting_on_one_job(daemon, root):
    """Plan B rev 4, layer 2: one waiter per job, so two hooks never both block."""
    daemon({"list": lambda request: {"jobs": [running_job()]},
            "wait": lambda request: {"timeout": True}})
    held = hooks.Lease(root, JOB)
    assert held.acquire() is True
    try:
        stderr = io.StringIO()
        clock = Clock()
        # Every candidate is leased, so the hook has nothing to wait on and
        # returns at once rather than queueing behind the holder.
        assert hooks.post_tool_use(
            payload("PostToolUse", tool_name="Bash",
                    tool_input={"command": "subfleet run -p p.md"},
                    tool_response=JOB),
            root, budget_s=30, stderr=stderr, now=clock, sleep=clock.sleep) == 0
        assert stderr.getvalue() == ""
        assert clock.now == 1000.0            # it never waited
    finally:
        held.release()


def test_a_released_lease_is_available_again(root):
    """C-5.3 the kernel drops a flock when its holder dies, so no reaping is needed."""
    first = hooks.Lease(root, JOB)
    assert first.acquire() is True
    assert hooks.Lease(root, JOB).acquire() is False
    first.release()
    second = hooks.Lease(root, JOB)
    assert second.acquire() is True
    second.release()


def test_a_lease_is_dropped_when_its_holder_exits(root):
    """C-5.3 the kernel drops a flock when its HOLDER dies, so a hook killed
    with its session leaves no lease to reap and no identity check is needed.

    Held by a real child process, because that is the claim: a thread would
    share this process and prove something weaker.
    """
    import subprocess
    import textwrap
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
            from subfleet import hooks
            lease = hooks.Lease({str(root)!r}, {JOB!r})
            assert lease.acquire()
            print("held", flush=True)
            time.sleep(30)
        """)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        blocked = hooks.Lease(root, JOB)
        assert blocked.acquire() is False, "a live holder must exclude a second waiter"
    finally:
        holder.kill()
        holder.wait(5)
    after = hooks.Lease(root, JOB)
    assert after.acquire() is True, "a dead holder's lease is gone with it"
    after.release()


def test_post_tool_use_ignores_tools_that_are_not_bash(daemon, root):
    """The entry is installed with `matcher: "Bash"`; the hook re-checks anyway."""
    server = daemon({"list": lambda request: {"jobs": [running_job()]}})
    assert hooks.post_tool_use(payload("PostToolUse", tool_name="Read"), root,
                               budget_s=1, stderr=io.StringIO()) == 0
    assert server.ops() == []


def test_post_tool_use_narrows_to_the_job_the_submission_created(daemon, root):
    """The lane brief's precise case: the ids `run` printed pick the job out."""
    waited: list[dict] = []
    daemon({
        "list": lambda request: {"jobs": [running_job(OTHER, "req-other"),
                                          running_job(JOB, "req-1")]},
        "wait": lambda request: waited.append(request.args) or {"jobs": [finished_job()]},
        "notice.pending": lambda request: {"notices": [notice(3, text="ok")]},
        "notice.mark": lambda request: {"notices": []},
    })
    clock = Clock()
    hooks.post_tool_use(
        payload("PostToolUse", tool_name="Bash",
                tool_input={"command": "subfleet run --task build --tier standard"},
                tool_response=f"submitted {JOB}"),
        root, budget_s=30, stderr=io.StringIO(), now=clock, sleep=clock.sleep)
    assert waited and waited[0]["job_ids"] == [JOB]


def test_post_tool_use_arms_session_wide_when_the_output_named_nothing(daemon, root):
    """Plan B rev 4: an unmatched submission still leaves the session-wide arm."""
    waited: list[dict] = []
    daemon({
        "list": lambda request: {"jobs": [running_job(OTHER, "req-other")]},
        "wait": lambda request: waited.append(request.args) or {"timeout": True},
    })
    clock = Clock()
    hooks.post_tool_use(
        payload("PostToolUse", tool_name="Bash",
                tool_input={"command": "subfleet run -p p.md --json > /dev/null"},
                tool_response=""),
        root, budget_s=2, stderr=io.StringIO(), now=clock, sleep=clock.sleep)
    assert waited and waited[0]["job_ids"] == [OTHER]


@pytest.mark.parametrize("command,expected", [
    ("subfleet run -p p.md", True),
    ("/Users/x/bin/subfleet run --task build --tier hard", True),
    ("SUBFLEET_ATTACHED_OK=1 subfleet run -p p.md", True),
    ("cd /repo && subfleet run -p p.md", True),
    ("nohup subfleet run -p p.md &", True),
    ("subfleet runs --mine", False),
    ("echo 'subfleet run' >> notes.md", False),
    ("bash -n bin/subfleet-run-wrapper", False),
    ("grep -n 'subfleet run' README.md", False),
])
def test_ran_subfleet_run_decides_the_way_v1s_guard_decides(command, expected):
    """v1 `bin/subfleet-hook:41-45`: command position, not a mention."""
    assert hooks.ran_subfleet_run(command) is expected


def test_tool_output_reads_both_spellings_of_the_output_field():
    """`claude-hooks.md` §2: the field table says `tool_response`, the page's own
    example says `tool_result`, and both literals are in the 2.1.260 binary."""
    assert JOB in hooks.tool_output({"tool_response": JOB})
    assert JOB in hooks.tool_output({"tool_result": JOB})
    assert JOB in hooks.tool_output({"tool_response": {"stdout": JOB}})
    assert JOB in hooks.tool_output({"tool_result": [{"text": JOB}]})
    assert hooks.tool_output({}) == ""


def test_ids_in_output_finds_job_and_request_ids():
    """C-17.4 `run` prints the job id on stdout and `request=<id>` on stderr."""
    job_ids, request_ids = hooks.ids_in_output(f"{JOB}\nrequest=req-1\n")
    assert job_ids == {JOB} and request_ids == {"req-1"}


def test_read_payload_never_raises_on_junk():
    """A hook that dies on a malformed payload blocks or spams every turn."""
    assert hooks.read_payload(io.StringIO("not json")) == {}
    assert hooks.read_payload(io.StringIO("")) == {}
    assert hooks.read_payload(io.StringIO("[1,2]")) == {}
    assert hooks.read_payload(io.StringIO('{"a":1}')) == {"a": 1}


# --- the `subfleet hook <event>` verb ----------------------------------------

def test_run_dispatches_each_event_spelling(daemon, root):
    """The v1 `bin/subfleet-hook` argument spellings stay accepted, so a
    half-migrated ~/.claude/settings.json still resolves."""
    daemon({"notice.pending": lambda request: {"notices": []}})
    for spelling in ("SessionStart", "session-start", "sessionstart",
                     "UserPromptSubmit", "user-prompt"):
        stream = io.StringIO(json.dumps(payload("SessionStart")))
        assert hooks.run(spelling, stream=stream, root=root) == 0


def test_run_rejects_an_unknown_event(root, capsys):
    """C-17.3 exit 2 is invalid input; the message names the three events."""
    assert hooks.run("PreToolUse", stream=io.StringIO("{}"), root=root) == 2
    assert "PostToolUse" in capsys.readouterr().err


def test_the_cli_verb_reaches_the_hook_module(root, monkeypatch, capsys):
    """C-17.1 `subfleet hook <event>` is the one binary all three entries call."""
    from subfleet import cli
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload("SessionStart"))))
    assert cli.main(["hook", "SessionStart"]) == 0


# --- settings entries ---------------------------------------------------------

def settings_file(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(data, indent=2))
    return path


def test_plan_proposes_the_three_entries_and_writes_nothing(tmp_path):
    """C-15.2 the three delivery entries; nothing touches the file to find out."""
    path = settings_file(tmp_path, {"statusLine": {"type": "command"}})
    before = path.read_text()
    report = hooks.plan(path, command="/bin/sf hook", timeout=600)
    assert report["ok"] and set(report["changed_events"]) == {
        "SessionStart", "UserPromptSubmit", "PostToolUse"}
    assert path.read_text() == before
    groups = report["proposed"]["hooks"]
    assert groups["PostToolUse"][0]["matcher"] == "Bash"
    entry = groups["PostToolUse"][0]["hooks"][0]
    assert entry["asyncRewake"] is True and entry["timeout"] == 600
    assert entry["command"] == "/bin/sf hook PostToolUse"
    # `claude-hooks.md` §1: the harness lowers the command default to 30 s on
    # UserPromptSubmit, so asking for more there would be a lie in the file.
    assert groups["UserPromptSubmit"][0]["hooks"][0]["timeout"] == 30
    assert "statusLine" in report["proposed"]        # nothing else is disturbed


def test_plan_is_idempotent(tmp_path):
    """A second `--hooks` is a no-op, so it is safe to run from a script."""
    path = settings_file(tmp_path, {})
    first = hooks.apply(path, command="/bin/sf hook")
    assert first["written"] is True
    assert hooks.plan(path, command="/bin/sf hook")["changed_events"] == []
    assert hooks.installed(path, command="/bin/sf hook")["matches"] is True


def test_v1_entries_are_reported_and_never_rewritten(tmp_path):
    """v1's PreToolUse guard is the only thing enforcing the front-door rule
    inside a session until v1 is uninstalled, so v2 leaves every v1 entry alone."""
    v1 = {"hooks": {
        "PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "~/cos/subfleet/bin/subfleet-hook pre-bash"}]}],
        "SessionStart": [{"hooks": [
            {"type": "command", "command": "~/cos/subfleet/bin/subfleet-hook session-start"}]}],
    }}
    path = settings_file(tmp_path, v1)
    report = hooks.plan(path, command="/bin/sf hook")
    assert set(report["v1_entries"]) == {"PreToolUse", "SessionStart"}
    hooks.apply(path, command="/bin/sf hook")
    after = json.loads(path.read_text())
    assert after["hooks"]["PreToolUse"] == v1["hooks"]["PreToolUse"]
    commands = [hook["command"] for group in after["hooks"]["SessionStart"]
                for hook in group["hooks"]]
    assert "~/cos/subfleet/bin/subfleet-hook session-start" in commands
    assert "/bin/sf hook SessionStart" in commands


def test_apply_replaces_v2s_own_entry_instead_of_stacking_it(tmp_path):
    """Running `--hooks` twice with a different command must not leave two."""
    path = settings_file(tmp_path, {})
    hooks.apply(path, command="/old/sf hook")
    hooks.apply(path, command="/new/sf hook")
    commands = [hook["command"]
                for group in json.loads(path.read_text())["hooks"]["SessionStart"]
                for hook in group["hooks"]]
    assert commands == ["/new/sf hook SessionStart"]


def test_apply_keeps_a_backup(tmp_path):
    """~/.claude/settings.json is hand-edited; a rewrite keeps the previous bytes."""
    path = settings_file(tmp_path, {"statusLine": {"type": "command"}})
    before = path.read_text()
    written = hooks.apply(path, command="/bin/sf hook")
    assert Path(written["backup"]).read_text() == before


def test_plan_reports_a_broken_settings_file_instead_of_overwriting_it(tmp_path):
    """A file that will not parse is a thing to fix, never a thing to replace."""
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    report = hooks.plan(path)
    assert report["ok"] is False and "path" in report
    assert path.read_text() == "{not json"


def test_daemon_install_hooks_prints_the_diff_and_writes_nothing_on_dry_run(
        tmp_path, monkeypatch, capsys):
    """The lane's own rule: the diff is printed before anything is written."""
    from subfleet import cli
    path = settings_file(tmp_path, {})
    monkeypatch.setenv(hooks.SETTINGS_ENV, str(path))
    assert cli.main(["daemon", "install", "--hooks", "--dry-run"]) == 0
    captured = capsys.readouterr()
    assert "+" in captured.out and "PostToolUse" in captured.out
    assert "would write" in captured.err
    assert json.loads(path.read_text()) == {}


def test_daemon_install_hooks_writes_after_printing_the_diff(tmp_path, monkeypatch,
                                                             capsys):
    """C-17.4 the path written goes to stdout; the prose goes to stderr."""
    from subfleet import cli
    path = settings_file(tmp_path, {})
    monkeypatch.setenv(hooks.SETTINGS_ENV, str(path))
    assert cli.main(["daemon", "install", "--hooks"]) == 0
    captured = capsys.readouterr()
    assert str(path) in captured.out and "PostToolUse" in captured.out
    assert set(json.loads(path.read_text())["hooks"]) == {
        "SessionStart", "UserPromptSubmit", "PostToolUse"}
    assert cli.main(["daemon", "install", "--hooks"]) == 0
    assert "already matches" in capsys.readouterr().err


def test_hook_command_prefers_this_interpreter(monkeypatch):
    """A bare `subfleet` on PATH may be v1; the running interpreter never is."""
    monkeypatch.delenv("SUBFLEET_HOOK_COMMAND", raising=False)
    assert hooks.hook_command().endswith("-m subfleet hook")
    monkeypatch.setenv("SUBFLEET_HOOK_COMMAND", "/x/sf hook")
    assert hooks.hook_command() == "/x/sf hook"


def test_timeout_is_explicit_and_overridable(monkeypatch):
    """Plan B rev 4: the entry's timeout and this process's budget move together."""
    monkeypatch.delenv("SUBFLEET_HOOK_TIMEOUT_S", raising=False)
    assert hooks.timeout_s() == 600
    monkeypatch.setenv("SUBFLEET_HOOK_TIMEOUT_S", "45")
    assert hooks.timeout_s() == 45
    assert hooks.desired_groups("/x/sf hook")["PostToolUse"]["hooks"][0]["timeout"] == 45
    monkeypatch.setenv("SUBFLEET_HOOK_TIMEOUT_S", "junk")
    assert hooks.timeout_s() == 600
