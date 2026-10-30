"""Every Claude launch argv this tree builds, over its builders' inputs (D-WATCHDOG1, d1031).

A case names one launch path and its inputs. `build(case, scratch)` runs that path's
own builder in a scratch folder, the way the daemon, the timers, the gate and the
conversation service call it, and returns the argv with the folder spelled
`<scratch>`. Nothing is spawned: the enrolment, probe and heal turns get a runner or
`popen` that records the argv and answers nothing, and no profile request leaves
(the opener answers 503).

The module imports only the standard library and `subfleet`, so the same cases build
against another checkout's package:

    python -I tests/lane_settings_cases.py <checkout> <scratch>  > argv.json

That is how `tests/fixtures/lane_settings/argv-676906f85.json` was recorded, at
676906f85, before any launch carried the override, and how
`tests/unit/test_lane_settings.py` checks the recording against that commit when the
commit is at hand.
"""

from __future__ import annotations

import itertools
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

#: The variable d1031 turns off, and the value every launch's settings give it.
WATCHDOG = "CLAUDE_CODE_RETRY_WATCHDOG"
OFF = "0"

SESSION = "00000000-0000-4000-8000-00000000d031"
MODEL = "claude-opus-5-5"
HAIKU = "claude-haiku-4-5-20251001"
TOKEN = "SECRET7-lane-token"            # a dummy: nothing here reads a credential
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
PLACEHOLDER = "<scratch>"

#: Every launch path, by the builder that makes its argv:
#: job/resume `ClaudeAdapter.build_launch`/`resume_launch` (detached jobs, writable and
#: read-only, review and build); probe the daemon's admission probe (C-11.4) and timer
#: its keepalive and heal turns (C-6.14), both `_execute_probe` -> `build_launch`; gate a
#: gate round's job (`gate.round.prepare` -> `build_launch`); turn
#: `conversations.launch.claude_launch` -> `claude_turn.argv`; enroll, probe-turn and
#: probe-outcome `ClaudeAdapter._turn_argv` through `enroll`, `probe` /
#: `probe_with_model` and `probe_outcome`; heal `claude_cards.heal_turn` (C-23.47).
PATHS = ("job", "resume", "probe", "timer", "gate", "turn", "enroll", "probe-turn", "probe-outcome", "heal")

SANDBOXES = ("read-only", "workspace-write")
TASKS = ("build", "review", None)
EFFORTS = (None, "high")
PERMISSIONS = ("ask", "accept-edits", "bypass", "read-only")
TURN_EFFORTS = (None, "high", "ultracode")


def case_id(case: dict) -> str:
    return "|".join([case["path"], *(f"{key}={case[key]}" for key in sorted(case) if key != "path")])


def cases() -> list[dict]:
    """The whole input grid of every path. An isolated review is read-only by rule
    (`validate_isolated_review`) and never resumes, so neither appears."""
    out: list[dict] = []
    for sandbox, task, isolated, effort, tmp, mcp in itertools.product(
            SANDBOXES, TASKS, (False, True), EFFORTS, (False, True), (False, True)):
        if isolated and sandbox != "read-only":
            continue
        out.append(dict(path="job", sandbox=sandbox, task=task, isolated=isolated, effort=effort, tmp=tmp, mcp=mcp))
    for sandbox, task, tmp, mcp, model in itertools.product(SANDBOXES, TASKS, (False, True), (False, True),
                                                            (False, True)):
        out.append(dict(path="resume", sandbox=sandbox, task=task, tmp=tmp, mcp=mcp, model=model))
    for sandbox, task, effort in itertools.product(SANDBOXES, ("build", "review"), (None, "low")):
        out.append(dict(path="probe", sandbox=sandbox, task=task, effort=effort))
    for purpose, effort in itertools.product(("keepalive", "heal"), (None, "low")):
        out.append(dict(path="timer", purpose=purpose, effort=effort))
    for kind, effort in itertools.product(("plan", "pr"), EFFORTS):
        out.append(dict(path="gate", gate=kind, effort=effort))
    for permission, fast, effort, resume, tmp in itertools.product(
            PERMISSIONS, (False, True), TURN_EFFORTS, (False, True), (False, True)):
        out.append(dict(path="turn", permission=permission, fast=fast, effort=effort, resume=resume, tmp=tmp))
    out.append(dict(path="enroll"))
    for model in (HAIKU, MODEL):
        out.append(dict(path="probe-turn", model=model))
    out.append(dict(path="probe-outcome"))
    out.append(dict(path="heal"))
    return out


def read_only(case: dict) -> bool:
    """Whether the case's launch is read-only: it carries the adapter's read-only flags."""
    if case["path"] in ("job", "resume"):
        return case["sandbox"] == "read-only"
    if case["path"] == "turn":
        return case["permission"] == "read-only"
    return case["path"] in ("probe", "timer", "gate")


def publish_hook(case: dict) -> bool:
    """Whether the case's launch carries the review publish hook (C-12.4)."""
    return case["path"] in ("job", "resume") and case["task"] == "review" and case["sandbox"] == "workspace-write"


def expected_settings(case: dict) -> dict:
    """What a case's command-line settings held before the override (I2), by key,
    the publish hook aside (`publish_hook`): an independent statement of
    `claude_turn.argv` and `review_publish.claude_settings_args`."""
    if case["path"] != "turn":
        return {}
    settings: dict = {}
    if case["permission"] != "read-only":
        settings["disableAllHooks"] = False
    if case["fast"]:
        settings["fastMode"] = True
    if case["effort"] == "ultracode" and case["permission"] != "read-only":
        settings["ultracode"] = True
    return settings


# --- the builders, as their callers call them --------------------------------


def _no_profile(_request, _timeout):
    return 503, b""


def _lane():
    from subfleet.contracts import Credential, Lane, LaneOwner
    return Lane(lane_id="claude-1", provider="claude", account_key="claude:lane@example.org",
                credential=Credential(provider="claude", ref="claude-quota-lane@example.org", kind="keychain-token"),
                home=None, owner=LaneOwner.V2, desktop=False, identity="acct-uuid:org-uuid",
                label="lane@example.org")


def _adapter(scratch: Path, runner=None):
    from subfleet.adapters.claude import ClaudeAdapter
    return ClaudeAdapter(claude_bin="claude", runner=runner, now=lambda: NOW, new_session_id=lambda: SESSION,
                         projects_dir=scratch / "projects", profile_opener=_no_profile, usage_opener=_no_profile)


def _credentials(scratch: Path, tmp: bool) -> dict[str, str]:
    env = {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}
    if tmp:
        env["TMPDIR"] = str(scratch / "attempt-tmp")
    return env


def _job(scratch: Path, case: dict):
    from subfleet.contracts import JobSpec, Sandbox
    work = scratch / "work"
    work.mkdir(parents=True, exist_ok=True)
    prompt = scratch / "prompt.md"
    prompt.write_text("Do the thing.\n", encoding="utf-8")
    review_root = None
    if case.get("isolated"):
        review_root = str(scratch / "review")
        Path(review_root).mkdir(exist_ok=True)
    servers, config = (), None
    if case.get("mcp"):
        path = scratch / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"docs": {"command": "/usr/bin/true"}}}), encoding="utf-8")
        servers, config = ("docs",), str(path)
    return JobSpec(request_id="req-lane-settings", kind="dispatch", workdir=str(work), prompt_path=str(prompt),
                   task=case["task"], tier="standard", pinned_model=None, pinned_lane=None,
                   sandbox=Sandbox(case["sandbox"]), out_path=None, name="lane-settings",
                   isolated_review=bool(case.get("isolated")), review_root=review_root,
                   mcp_servers=servers, mcp_config=config)


def _daemon_spec(scratch: Path, row: dict, **overrides):
    """`Daemon._spec`, the daemon's own job row -> JobSpec, with only what it reads."""
    from subfleet.daemon import Daemon
    return Daemon._spec(SimpleNamespace(policy={}, root=scratch), row, **overrides)


def _probe_launch(scratch: Path, row: dict, effort: str | None):
    """`Daemon._execute_probe`'s spec and `build_launch` call (C-11.4, C-6.14)."""
    from subfleet.contracts import Sandbox
    directory = scratch / "lanes" / "claude-1" / "probes" / "token"
    directory.mkdir(parents=True, exist_ok=True)
    prompt = directory / "prompt.md"
    prompt.write_text("Reply with exactly OK. Do not use tools.\n", encoding="utf-8")
    spec = _daemon_spec(scratch, row, kind="probe", workdir=str(directory), prompt_path=str(prompt),
                        sandbox=Sandbox.READ_ONLY, out_path=None, mcp_servers=(), mcp_config=None)
    credential_env = {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN, "TMPDIR": str(scratch / "probe-tmp")}
    return _adapter(scratch).build_launch(spec, "probe:job:token", directory, _lane(), credential_env, HAIKU,
                                          effort, prompt, None)


def _recorded_turn(scratch: Path, call) -> list[str]:
    """The argv of one `_run_turn`, recorded by the adapter's own runner seam."""
    seen: list[list[str]] = []

    def runner(argv, **_kwargs):
        seen.append([str(arg) for arg in argv])
        return SimpleNamespace(returncode=1, stdout="", stderr="")
    adapter = _adapter(scratch, runner=runner)
    try:
        call(adapter)
    except Exception:           # enrolment refuses a turn that never authenticated; the argv is what counts
        pass
    turns = [argv for argv in seen if argv[:2] == ["claude", "-p"]]
    assert len(turns) == 1, turns
    return turns[0]


def build(case: dict, scratch: Path) -> list[str]:
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    path = case["path"]
    if path == "job":
        job = _job(scratch, case)
        launch = _adapter(scratch).build_launch(job, "job-1/a1", scratch / "attempt", _lane(),
                                                _credentials(scratch, case["tmp"]), MODEL, case["effort"],
                                                Path(job.prompt_path), None)
        argv = launch.argv
    elif path == "resume":
        job = _job(scratch, case)
        launch = _adapter(scratch).resume_launch(job, "job-1/a2", scratch / "attempt", _lane(),
                                                 _credentials(scratch, case["tmp"]), SESSION,
                                                 Path(job.prompt_path), None,
                                                 model_id=MODEL if case["model"] else None)
        argv = launch.argv
    elif path == "probe":
        # C-11.4: the waiting job's own row, which `_execute_probe` turns read-only.
        row = {"job_id": "job-1", "request_id": "req", "kind": "dispatch", "workdir": str(scratch),
               "prompt_path": str(scratch / "prompt.md"), "sandbox": case["sandbox"], "exclusions": "[]",
               "task": case["task"], "tier": "standard", "pinned_model": None, "pinned_lane": None,
               "name": "probed", "out_path": None, "mcp_servers": "[]"}
        argv = _probe_launch(scratch, row, case["effort"]).argv
    elif path == "timer":
        # `Daemon._timer_turn`'s row, the same for a keepalive and a heal turn.
        directory = scratch / "lanes" / "claude-1" / "probes" / "token"
        row = {"job_id": "timer-token", "request_id": "token", "kind": "probe", "workdir": str(directory),
               "prompt_path": str(directory / "prompt.md"), "sandbox": "read-only", "exclusions": "[]",
               "task": None, "tier": None, "pinned_model": None, "pinned_lane": None, "name": None,
               "out_path": None}
        argv = _probe_launch(scratch, row, case["effort"]).argv
    elif path == "gate":
        from subfleet.gate.round import prepare
        work = scratch / "work"
        work.mkdir(exist_ok=True)
        root = scratch / "state"
        state = {"id": "gate-1", "kind": case["gate"], "workdir": str(work), "peer": MODEL}
        record = {"number": 1, "attempt_id": "0123456789abcdef", "revision": {"sha256": "0" * 64}}
        args = prepare(root, state, record, b"the artifact\n")
        row = {**asdict(args), "job_id": "job-gate-1", "exclusions": json.dumps(list(args.exclusions)),
               "mcp_servers": "[]"}
        spec = _daemon_spec(root, row)
        argv = _adapter(scratch).build_launch(spec, "job-gate-1/a1", scratch / "attempt", _lane(),
                                              {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}, MODEL, case["effort"],
                                              Path(spec.prompt_path), None).argv
    elif path == "turn":
        from subfleet.conversations.launch import claude_launch
        work = scratch / "work"
        work.mkdir(exist_ok=True)
        turn = {"provider": "claude", "conversation_id": "conversation-1", "message_id": "message-1",
                "text": "continue", "cwd": str(work),
                "settings": {"model": "opus", "effort": case["effort"], "permission": case["permission"],
                             "fast": case["fast"]},
                ("native_session_id" if case["resume"] else "new_session_id"): SESSION}
        argv = claude_launch(turn, attempt_id="turn-job/a1", attempt_dir=scratch / "attempt", lane=_lane(),
                             credential_env=_credentials(scratch, case["tmp"]), model_id=MODEL,
                             adapter=_adapter(scratch)).argv
    elif path == "enroll":
        from subfleet.contracts import Credential
        home = scratch / "lane-home"
        home.mkdir(exist_ok=True)
        argv = _recorded_turn(scratch, lambda a: a.enroll(Credential(provider="claude", ref=str(home),
                                                                     kind="home")))
    elif path == "probe-turn":
        model = case["model"]
        argv = _recorded_turn(scratch, (lambda a: a.probe(_lane(), {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}))
                              if model == HAIKU else
                              (lambda a: a.probe_with_model(_lane(), {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}, model)))
    elif path == "probe-outcome":
        argv = _recorded_turn(scratch, lambda a: a.probe_outcome(_lane(), {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN},
                                                                 MODEL))
    elif path == "heal":
        argv = heal_argv(scratch)
    else:
        raise ValueError(f"unknown launch path {path!r}")
    return normalize(argv, scratch)


def heal_argv(scratch: Path, home: Path | None = None) -> list[str]:
    """`claude_cards.heal_turn`'s argv, from a `popen` that records it. Its child is no
    process: `_kill_group` is replaced for the call, so nothing is signalled."""
    from subfleet import claude_cards
    seen: list[list[str]] = []

    class Child:
        pid, returncode = -1, 0

        def __init__(self, argv, **_kwargs):
            seen.append([str(arg) for arg in argv])

        def communicate(self, timeout=None):
            return "ok", ""
    original = claude_cards._kill_group
    claude_cards._kill_group = lambda pid, sig=None: None
    try:
        claude_cards.heal_turn(home or scratch / "login", claude_bin="claude", model=HAIKU,
                               prompt="Reply with exactly: ok", popen=Child)
    finally:
        claude_cards._kill_group = original
    assert len(seen) == 1, seen
    return seen[0]


def normalize(argv, scratch: Path) -> list[str]:
    forms = sorted({str(scratch), os.path.realpath(scratch)}, key=len, reverse=True)
    out = []
    for arg in argv:
        arg = str(arg)
        for form in forms:
            arg = arg.replace(form, PLACEHOLDER)
        out.append(arg)
    return out


# --- the settings a launch gives on its command line -------------------------


def settings_payloads(argv) -> list[str]:
    """Every `--settings` value in `argv`, in order (both spellings)."""
    argv = list(argv)
    found = []
    for index, arg in enumerate(argv):
        if arg == "--settings":
            found.append(argv[index + 1] if index + 1 < len(argv) else "")
        elif arg.startswith("--settings="):
            found.append(arg.split("=", 1)[1])
    return found


def without_override(argv) -> list[str]:
    """`argv` with the override taken back out of its one `--settings`: the variable
    removed from `env`, `env` dropped once empty, and the flag dropped once its
    settings are empty. The differential's view of a launch before the change."""
    argv = list(argv)
    index = argv.index("--settings")
    settings = json.loads(argv[index + 1])
    env = dict(settings.get("env") or {})
    env.pop(WATCHDOG, None)
    if env:
        settings["env"] = env
    else:
        settings.pop("env", None)
    if not settings:
        return argv[:index] + argv[index + 2:]
    return argv[:index + 1] + [json.dumps(settings, separators=(",", ":"))] + argv[index + 2:]


def main(argv: list[str]) -> int:
    checkout, scratch = Path(argv[0]).resolve(), Path(argv[1])
    sys.path.insert(0, str(checkout))
    import subfleet
    if not Path(subfleet.__file__).resolve().is_relative_to(checkout):
        print(f"subfleet resolved to {subfleet.__file__}, not under {checkout}", file=sys.stderr)
        return 2
    built = {}
    for number, case in enumerate(cases()):
        built[case_id(case)] = build(case, scratch / f"case-{number:03d}")
    json.dump({"cases": built}, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
