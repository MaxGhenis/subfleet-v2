"""D-LOG1 (C-25.7, C-26.10): what the daemon's real writers put in daemon.log is
scrubbed.

Each case runs a real path. A turn runner, the conversation service, its wake
engine and the daemon log through the handler a real `Daemon` makes. A refused
`suggestion.start` goes over the wire. Real daemon processes are started as
`daemon start` and as the launchd plist starts them, and their uncaught
exceptions reach daemon.log through stderr. A lint keeps each module's own call
sites honest as well, so the rule does not rest on the handler alone.

Every secret is a `SECRET7…` dummy, kept out of every `raise` line a traceback
shows, so no source line decides a case.
"""

from __future__ import annotations

import ast
import concurrent.futures
import faulthandler
import json
import logging
import os
import plistlib
import re
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from subfleet import cli, folders, ids, scheduler
from subfleet import daemon as daemon_module
from subfleet.adapters.registry import register
from subfleet.conversations.runner import TurnRunner
from subfleet.conversations.store import ConversationStore
from subfleet.conversations.turn import TurnSpec
from subfleet.daemon import Daemon
from subfleet.daemon_log import RECORD_SCRUB_MAX
from subfleet.procs import Containment
from tests.fake.conftest import Harness
from tests.fake_adapter import FakeAdapter
from tests.unit.test_conversation_service import SETTINGS, svc  # noqa: F401 - svc: the conversation service fixture
from tests.unit.test_protocol_secrecy import wire_reply
from tests.unit.test_round10_refusal_context import SEARCHABLE, UNREADABLE
from tests.unit.test_suggestion_secret_properties import AGENT, fixture, origin_turn

REPO = Path(__file__).resolve().parents[2]
MID = "7f1c9a0e-1111-4222-8333-444455556666"
SID = "0b0e0f00-aaaa-4bbb-8ccc-dddddddddddd"


@pytest.fixture
def daemon_log(tmp_path, monkeypatch):
    """A real `Daemon` (process reads stubbed, as tests/fake/test_state_contract.py
    stubs them), its logger, and what its own handler wrote to its daemon.log."""
    root = tmp_path / "daemon"
    root.mkdir()
    harness = Harness(root)
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "unit-test-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "unit-test-start")
    monkeypatch.setattr(daemon_module.procs, "same_process", lambda *args: False)
    monkeypatch.setattr(daemon_module.procs, "snapshot", lambda: daemon_module.procs.ProcessTable({
        os.getpid(): (1, os.getpgrp(), "S", "unit-test-start")}, "unit-test-boot"))
    monkeypatch.setattr(daemon_module.procs, "identity", lambda pid: daemon_module.procs.snapshot().identity(pid))
    monkeypatch.setattr(daemon_module.procs, "group_members", lambda *args: {})
    monkeypatch.setattr(daemon_module.procs, "containment", lambda *args, **kwargs: Containment())
    register("codex", FakeAdapter)
    daemon = Daemon(harness.root)

    def refuse_real_launch(*args):
        raise AssertionError("these cases never launch a guardian")
    monkeypatch.setattr(daemon, "_launch", refuse_real_launch)

    def read() -> str:
        daemon._log_handler.flush()
        return (harness.root / "daemon.log").read_text()
    try:
        yield SimpleNamespace(daemon=daemon, log=daemon.log, read=read)
    finally:
        daemon.close()


def _runner(store, tmp_path, log, conversation_id: str = "cv-x") -> TurnRunner:
    spec = TurnSpec(provider="claude", message_id=MID, text="hi", model_id="opus[1m]", permission="ask",
                    native_session_id=None, new_session_id=SID)
    return TurnRunner(store=store, attempt={"attempt_id": "job/a1", "lane_id": "claude-1"}, spec=spec,
                      conversation_id=conversation_id, attempt_dir=tmp_path / "a1", control_socket=str(tmp_path / "none.sock"),
                      on_outcome=lambda result: None, on_contain=lambda aid: None, log=log)


def _raise(error):
    def raising(*args, **kwargs):
        raise error
    return raising


# --- the raw sites, one per module, through the daemon's handler ---------------------


def test_the_reviewers_turn_runner_failure_leaves_no_secret(daemon_log):
    """#225 review r1's executed input: `TurnRunner._failed` wrote the secret raw."""
    runner = object.__new__(TurnRunner)
    runner.log, runner.attempt_id = daemon_log.log, "job/a1"
    runner._failed(RuntimeError("app-server said: password='" + "SECRET7" + "runner9\n\""))
    text = daemon_log.read()
    assert "turn runner job/a1 failed: RuntimeError: app-server said: password=" in text
    assert "SECRET7runner9" not in text


def test_a_turn_runner_whose_loop_raises_leaves_no_secret(daemon_log, tmp_path):
    """runner.py's `_failed`, reached as a runner defect reaches it: its loop raised."""
    store = ConversationStore(tmp_path / "conversations")
    try:
        cid = store.create_conversation(provider="claude", workspace=str(tmp_path), workspace_kind="in-place",
                                        settings=SETTINGS, origin="new")[0]["conversation_id"]
        store.submit_message(conversation_id=cid, message_id=MID, after_message_id=None, text="hi",
                             attachments=[], settings=SETTINGS)
        runner = _runner(store, tmp_path, daemon_log.log, conversation_id=cid)
        runner.driver.start = _raise(RuntimeError("driver said: api_key = " + "SECRET7" + "loop9"))
        runner._run()
    finally:
        store.close()
    text = daemon_log.read()
    assert "turn runner job/a1 failed: RuntimeError: driver said: api_key = [REDACTED]" in text
    assert "SECRET7loop9" not in text


def test_an_approval_the_driver_refuses_leaves_no_secret(daemon_log, tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    try:
        runner = _runner(store, tmp_path, daemon_log.log)
        runner.driver.respond = _raise(ValueError("no such request: token=" + "SECRET7" + "approval9"))
        runner._respond("req-1", "allow", None, None)
    finally:
        store.close()
    text = daemon_log.read()
    assert "approval req-1 on job/a1 refused by the driver: ValueError: no such request: token=[REDACTED]" in text
    assert "SECRET7approval9" not in text


def test_a_handoff_fence_lift_that_fails_leaves_no_secret(svc, daemon_log, monkeypatch):
    """service.py's handoff failure path, through the daemon's logger as the service holds it."""
    svc.log = daemon_log.log
    monkeypatch.setattr(svc, "_lift_fence", _raise(RuntimeError("store said: secret: " + "SECRET7" + "fence9")))
    svc._lift_fence_quietly("cv-x", "handoff:abc")
    text = daemon_log.read()
    assert "lifting handoff fence handoff:abc of cv-x failed: RuntimeError: store said: secret: [REDACTED]" in text
    assert "SECRET7fence9" not in text


def test_a_pr_wake_worker_that_failed_leaves_no_secret(svc, daemon_log):
    """wakes.py's control tick reads a finished poll that raised."""
    svc.log = daemon_log.log
    failed = concurrent.futures.Future()
    failed.set_exception(OSError("gh said: password=" + "SECRET7" + "wake9"))
    wakes = svc.wakes
    wakes._poll_future = failed
    wakes._next_poll = wakes._next_completions = wakes._next_requests = float("inf")
    wakes.control_tick()
    text = daemon_log.read()
    assert "PR wake worker failed: OSError: gh said: password=[REDACTED]" in text
    assert "SECRET7wake9" not in text


def test_a_pin_left_as_it_is_leaves_no_secret(daemon_log, monkeypatch, tmp_path):
    """daemon.py's pin repair: a pin that routes nowhere is left for admission."""
    daemon = daemon_log.daemon
    daemon.store.add_job(job_id="20261018-000000-pin-0001", request_id="pin-request", payload_digest="d",
                         kind="job", workdir=str(tmp_path), prompt_path="p.md", sandbox="read-only", name="pin",
                         in_place=1, max_attempts=1, pinned_lane="old-alias")
    monkeypatch.setattr(daemon_module.scheduler, "resolve_lane", _raise(scheduler.RouteError(
        "pin old-alias names several lanes; roster said token=" + "SECRET7" + "pin9")))
    daemon._canonicalize_pins()
    text = daemon_log.read()
    assert ("job 20261018-000000-pin-0001 pin 'old-alias' was left as it is: RouteError: pin old-alias names "
            "several lanes; roster said token=[REDACTED]") in text
    assert "SECRET7pin9" not in text


CARELESS = "password=SECRET7careless9"


def test_a_secret_a_careless_site_logs_raw_is_scrubbed_by_the_daemons_handler(daemon_log):
    """The handler a real Daemon makes is the layer under every site: a secret a
    site passes raw, on its line or in its traceback, is scrubbed there."""
    try:
        raise RuntimeError(CARELESS)
    except RuntimeError as exc:
        daemon_log.log.warning("a site said: %s", exc)          # the site lint flags this in subfleet/
        daemon_log.log.exception("and its traceback")
    text = daemon_log.read()
    assert "a site said: password=[REDACTED]\n" in text
    assert "and its traceback\nTraceback (most recent call last):\n" in text
    assert "RuntimeError: password=[REDACTED]" in text and "SECRET7careless9" not in text


def test_faulthandler_writes_frames_only(daemon_log):
    """Kept as it is (C-3.6): faulthandler writes each frame's file, line and
    function, never the exception being handled, a local or a source line."""
    stream = daemon_log.daemon._log_handler.stream
    secret = "SECRET7" + "frame9"
    try:
        raise RuntimeError("password=" + secret)
    except RuntimeError:
        held = {"password": secret}
        stream.flush()
        faulthandler.dump_traceback(file=stream, all_threads=False)
    text = daemon_log.read()
    assert "in test_faulthandler_writes_frames_only" in text
    assert secret not in text and "RuntimeError" not in text and "raise RuntimeError" not in text
    assert held
    if sys.version_info >= (3, 14):
        # And, for all threads (as `daemon stacks` dumps them), each thread's name
        # (review r1 of #233, P3): fixed or ids only, below.
        import threading
        release = threading.Event()
        named = threading.Thread(target=release.wait, name="subfleet-probe", daemon=True)
        named.start()
        try:
            stream.flush()
            faulthandler.dump_traceback(file=stream, all_threads=True)
        finally:
            release.set()
            named.join(30)
        assert "[subfleet-probe] (most recent call first)" in daemon_log.read()


#: A name a thread is given: fixed words (a pool's prefix too, to which the pool adds
#: `_<n>`). Without one, Python names it `Thread-<n> (<function>)`.
FIXED_THREAD_NAME = re.compile(r"[a-z][a-z0-9-]*\Z")
#: The one computed name: a turn runner's, its attempt's id (C-1.2).
COMPUTED_THREAD_NAMES = {"f'turn:{self.attempt_id}'"}
#: That id: the job of a turn is named `turn-<conversation id>` (C-26.12), so ids only.
#: (`ids.job_id` cuts the slug at 40 characters, so a uuid's last ones may go.)
TURN_THREAD_NAME = re.compile(r"turn:\d{8}-\d{6}-turn-[0-9a-f-]+/a\d+\Z")


def test_every_daemon_thread_s_name_is_fixed_or_ids_only():
    """Review r1 of #233, P3: on Python 3.14 faulthandler also writes each thread's
    name (`Thread 0x… [name]`), which no scrubber reads. Every thread and pool
    `subfleet/` starts is named by a literal, by Python's default, or by the turn
    runner's `turn:<attempt id>`, whose job is named `turn-<conversation id>`; and
    nothing renames a thread later. So a name never carries text."""
    found = 0
    for path in sorted((REPO / "subfleet").rglob("*.py")):
        where = path.relative_to(REPO)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                assert callee != "setName", f"{where}:{node.lineno}"
                if callee not in ("Thread", "Timer", "ThreadPoolExecutor"):
                    continue
                found += 1
                for keyword in node.keywords:
                    if keyword.arg in ("name", "thread_name_prefix"):
                        value = keyword.value
                        if isinstance(value, ast.Constant) and isinstance(value.value, str):
                            assert FIXED_THREAD_NAME.match(value.value), f"{where}:{node.lineno}"
                        else:
                            assert ast.unparse(value) in COMPUTED_THREAD_NAMES, f"{where}:{node.lineno}"
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    assert not (isinstance(target, ast.Attribute) and target.attr == "name"
                                and isinstance(target.value, ast.Name) and "thread" in target.value.id.lower()), \
                        f"{where}:{node.lineno}"
    assert found >= 20
    assert TURN_THREAD_NAME.match("turn:" + ids.attempt_id(ids.job_id(f"turn-{uuid.uuid4()}"), 2))
    service = (REPO / "subfleet" / "conversations" / "service.py").read_text(encoding="utf-8")
    assert "name=f\"turn-{conversation['conversation_id']}\"" in service


# --- suggestion.start (C-26.10) ---------------------------------------------------


def test_a_refused_suggestion_start_logs_its_own_words_scrubbed(daemon_log, tmp_path, monkeypatch):
    """#225 review r2's input: a start widened to accept-edits, with `<HOME>/.claude`'s
    spelling unreadable (EACCES). The wire keeps the template; daemon.log now has
    the refusing op's own words, scrubbed, where nothing reached it before."""
    with fixture() as service:
        _, job = origin_turn(service)
        row = service.handle("suggestion.create", {
            "request_id": str(uuid.uuid4()), "title": "Fixture", "tldr": "Fixture", "brief": "Fixture",
            "calling_job": job, "workspace": str(service.folder / "work")}, AGENT)["suggestion"]
        home = tmp_path / ("home token=" + "SECRET7" + "homedir9")
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        real_spelling = folders.spelling

        def spelling(path):
            if os.fspath(path) == str(home / ".claude"):
                return str(path), "EACCES"
            return real_spelling(path)
        monkeypatch.setattr(folders, "spelling", spelling)
        service.log = daemon_log.log
        reply = wire_reply(service, op="suggestion.start", args={
            "suggestion_id": row["suggestion_id"], "permission": "accept-edits", "confirm_widen": True})
    assert reply["ok"] is False and reply["error"]["code"] == 7
    assert reply["error"]["message"] == UNREADABLE and reply["error"]["fix"] == SEARCHABLE
    shown = json.dumps(reply)
    assert "SECRET7" not in shown and ".claude" not in shown and "EACCES" not in shown and str(home) not in shown
    text = daemon_log.read()
    assert "request suggestion.start refused (ConversationError): cannot tell whether" in text
    assert "EACCES" in text and "SECRET7homedir9" not in text


# --- real daemon processes: uncaught exceptions reach daemon.log through stderr --------


def _subfleetd(monkeypatch) -> Path:
    """`daemon start` and the plist run this checkout's `subfleetd` beside the interpreter."""
    executable = Path(sys.executable).parent / "subfleetd"
    if not executable.is_file():
        pytest.skip("run uv sync --group dev to install subfleetd")
    found = subprocess.run([sys.executable, "-E", "-P", "-c", "import subfleet; print(subfleet.__file__)"],
                           capture_output=True, text=True, timeout=60).stdout.strip()
    if not found.startswith(str(REPO)):
        pytest.skip(f"the interpreter's subfleet is not this checkout's ({found or 'none'})")
    monkeypatch.delenv("SUBFLEET_DAEMON_BIN", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join([str(executable.parent), "/usr/bin", "/bin"]))
    assert cli._daemond_argv(Path("/x"))[:4] == [sys.executable, "-E", "-P", str(executable)]
    return executable


def _policy_naming_a_secret(root: Path) -> str:
    """policy.json with an admission key the daemon refuses at start, naming it: a
    `PolicyError` no code catches, so Python's own hook writes it to stderr."""
    secret = "SECRET7" + "policy9"
    policy = json.loads((REPO / "subfleet" / "default_policy.json").read_text())
    policy.setdefault("admission", {})["token=" + secret] = 1
    (root / "policy.json").write_text(json.dumps(policy))
    return secret


def _assert_uncaught_policy_error_scrubbed(text: str, secret: str) -> None:
    assert "uncaught exception\nTraceback (most recent call last):\n" in text
    assert "PolicyError: " in text and "admission.token=[REDACTED]" in text
    assert secret not in text


def test_under_daemon_start_an_uncaught_exception_reaches_daemon_log_scrubbed(root, monkeypatch, capsys):
    """`subfleet daemon start` points the daemon's stdout and stderr at daemon.log;
    the daemon fails to start on its policy, and the traceback is scrubbed."""
    _subfleetd(monkeypatch)
    secret = _policy_naming_a_secret(root)
    assert cli.main(["daemon", "start"]) == 69
    _assert_uncaught_policy_error_scrubbed((root / "daemon.log").read_text(), secret)
    assert secret not in capsys.readouterr().err           # the log tail `daemon start` prints


def test_under_launchd_an_uncaught_exception_reaches_daemon_log_scrubbed(root, monkeypatch):
    """The plist's own program, folder, environment and output paths, as launchd runs them."""
    _subfleetd(monkeypatch)
    secret = _policy_naming_a_secret(root)
    plist = plistlib.loads(cli._plist(root))
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(root / "daemon.log")
    env = {"HOME": os.environ["HOME"], **plist["EnvironmentVariables"]}
    with open(plist["StandardOutPath"], "ab") as out, open(plist["StandardErrorPath"], "ab") as err:
        done = subprocess.run(plist["ProgramArguments"], cwd=plist["WorkingDirectory"], env=env,
                              stdin=subprocess.DEVNULL, stdout=out, stderr=err, timeout=120)
    assert done.returncode == 1
    _assert_uncaught_policy_error_scrubbed((root / "daemon.log").read_text(), secret)


SITECUSTOMIZE = '''\
import sys
from pathlib import Path

if Path(sys.argv[0]).name == "subfleetd":
    import threading
    import warnings
    from subfleet import descriptors

    # Out of the `raise` lines, which each traceback shows (see the handler tests).
    THREAD = "worker said: client_secret: SECRET7thread9"
    FINALIZER = "finalizer said: password='SECRET7unraisable9'"
    WARNING = "policy said: api_key = SECRET7warning9"
    MAIN = "main said: Authorization: Bearer SECRET7mainthread9value"

    def fail():
        def work():
            raise RuntimeError(THREAD)
        thread = threading.Thread(target=work, name="subfleet-io_0")
        thread.start()
        thread.join()

        class Leak:
            def __del__(self):
                raise ValueError(FINALIZER)
        Leak()
        warnings.warn(WARNING, UserWarning)
        raise RuntimeError(MAIN)

    # The daemon's first step after its hooks: from here everything raises.
    descriptors.raise_open_file_limit = fail
'''


def test_a_daemon_process_writes_its_thread_unraisable_warning_and_main_failures_scrubbed(root, tmp_path, monkeypatch):
    """The real `subfleetd`, its stderr daemon.log: an uncaught exception in a thread,
    one in a finalizer, a warning and one in the main thread, each scrubbed."""
    executable = _subfleetd(monkeypatch)
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(SITECUSTOMIZE)
    env = {**os.environ, "PYTHONPATH": str(site), "SUBFLEET_HOME": str(root)}
    with open(root / "daemon.log", "ab") as log:
        done = subprocess.run([str(executable), "--state-root", str(root)], cwd=str(root), env=env,
                              stdin=subprocess.DEVNULL, stdout=log, stderr=log, timeout=120)
    text = (root / "daemon.log").read_text()
    assert done.returncode == 1, text
    for secret in ("SECRET7thread9", "SECRET7unraisable9", "SECRET7warning9", "SECRET7mainthread9value"):
        assert secret not in text, secret
    assert "Exception in thread subfleet-io_0\nTraceback (most recent call last):\n" in text
    assert "RuntimeError: worker said: client_secret: [REDACTED]" in text
    assert "Exception ignored" in text and "ValueError: finalizer said: password=" in text
    assert "UserWarning: policy said: api_key = [REDACTED]" in text
    assert "uncaught exception\nTraceback (most recent call last):\n" in text and "RuntimeError: main said:" in text


# --- the record's cut, through a real Daemon (review r2 of #233) -----------------------


def _raised(text: str) -> BaseException:
    try:
        raise RuntimeError(text)
    except RuntimeError as exc:
        return exc


def test_a_flag_whose_value_starts_the_kept_tail_stays_out_of_daemon_log(daemon_log):
    """Review r2's input, read back from a real daemon.log: it wrote
    `… [31,826 characters omitted] …\nSECRET7flag9 rest`. Folded, as both forms fold,
    `--password\nSECRET` is `--password SECRET`; no cut falls between them now."""
    from tests.unit.test_daemon_log_handler import FLAG_TEXT
    daemon_log.log.error("%s", FLAG_TEXT)
    text = daemon_log.read()
    assert "characters omitted] …" in text
    assert "SECRET7flag9" not in text


def _flag_traceback_lined_up_with_the_cut() -> BaseException:
    """An exception whose traceback, logged under the message `failed`, has its kept
    tail start at the flag's value: the first line start in its tail window."""
    share = RECORD_SCRUB_MAX - len("failed")             # `_within`: the message takes its length
    for count in range(4000, 4200):
        exc = _raised("w\n" * 20000 + "deploy --password\n" + "SECRET7flag9 rest\n" + "w\n" * count)
        tb = logging.Formatter().formatException((type(exc), exc, exc.__traceback__))
        at, window = tb.index("SECRET7flag9"), len(tb) - share // 2
        if at >= window > tb.rindex("\n", 0, at - 1):
            return exc
    raise AssertionError("no traceback lines up with the cut")


def test_a_flag_whose_value_starts_a_traceback_s_kept_tail_stays_out_of_daemon_log(daemon_log):
    exc = _flag_traceback_lined_up_with_the_cut()
    daemon_log.log.error("failed", exc_info=(type(exc), exc, exc.__traceback__))
    text = daemon_log.read()
    assert "failed\nTraceback (most recent call last):\n" in text and "characters omitted] …" in text
    assert "SECRET7flag9" not in text


@pytest.mark.parametrize("shape", ["token:\n", "token =\n  ", "Authorization: Bearer\n"])
def test_a_group_over_the_bound_is_written_to_daemon_log_scrubbed(daemon_log, shape):
    """Review r2's group: its own 1,200 lines fill the kept head and the kept tail starts
    inside its last member, so the cut kept no separator. It wrote
    `    | token:\n    [REDACTED] SECRET7grpcut9`."""
    from tests.unit.test_daemon_log_handler import over_bound_group
    group = over_bound_group(shape)
    daemon_log.log.error("failed", exc_info=(type(group), group, group.__traceback__))
    text = daemon_log.read()
    assert "characters omitted] …" in text and "member line" in text
    assert "SECRET7grpcut9" not in text


#: A process that installs the daemon's hooks and raises review r2's group uncaught.
#: The secret is built off the `raise` line, which the traceback shows.
GROUP_CHILD = """\
import sys

from subfleet import daemon_log

daemon_log.install_process_hooks()
shape = {"key": "token:\\n", "spaced": "token =\\n  ", "bearer": "Authorization: Bearer\\n"}[sys.argv[1]]
secret = "SECRET7" + "grpcut9"
member = RuntimeError("\\n".join(["member line"] * 1000) + "\\n" + shape + secret)
group = ExceptionGroup("\\n".join(["group context line"] * 1200), [member])
raise group
"""


@pytest.mark.parametrize("shape", ["key", "spaced", "bearer"])
def test_a_process_that_raises_a_group_over_the_bound_writes_it_scrubbed(tmp_path, shape):
    """A real child, its stderr a daemon.log, raising the group uncaught through
    `install_process_hooks`, as `daemon.main` installs them."""
    script = tmp_path / "child.py"
    script.write_text(GROUP_CHILD)
    with open(tmp_path / "daemon.log", "ab") as log:
        done = subprocess.run([sys.executable, str(script), shape], cwd=str(tmp_path), stdin=subprocess.DEVNULL,
                              stdout=log, stderr=log, timeout=120)
    text = (tmp_path / "daemon.log").read_text()
    assert done.returncode == 1, text[-2000:]
    assert "uncaught exception\n" in text and "ExceptionGroup" in text and "characters omitted] …" in text
    assert "SECRET7grpcut9" not in text, text[-600:]


# --- the call sites themselves ------------------------------------------------------

LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical", "log"})
#: What may carry a caught exception into a log call: the scrubbers, its class, its
#: errno or exit status.
SCRUBBERS = frozenset({"refusals.scrubbed_words", "refusals.scrubbed_line", "refusals.fixed_error",
                       "refusals.detail_line", "detail_line",
                       "refusals.class_name", "refusals.errno_text", "scrubbed_words", "suggestions.scrub_error",
                       "suggestions.scrubbed_line", "type"})
NUMBERS = frozenset({"errno", "returncode"})


def _exception_parameters(node) -> set[str]:
    names = set()
    for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
        annotation = ast.unparse(arg.annotation) if getattr(arg, "annotation", None) is not None else ""
        if arg.arg == "exc" or re.search(r"(Error|Exception)\b", annotation):
            names.add(arg.arg)
    return names


def _is_log_call(node: ast.Call) -> bool:
    if not (isinstance(node.func, ast.Attribute) and node.func.attr in LOG_METHODS):
        return False
    receiver = ast.unparse(node.func.value)
    return receiver.endswith("log") or "getLogger(" in receiver


def _raw(node, names) -> list[str]:
    if isinstance(node, ast.Call) and ast.unparse(node.func) in SCRUBBERS:
        return []
    if (isinstance(node, ast.Attribute) and node.attr in NUMBERS and isinstance(node.value, ast.Name)
            and node.value.id in names):
        return []
    if isinstance(node, ast.Name) and node.id in names:
        return [node.id]
    return [name for child in ast.iter_child_nodes(node) for name in _raw(child, names)]


def _functions(tree) -> dict[str, list]:
    functions: dict[str, list] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, []).append(node)
    return functions


def _bound(call: ast.Call, functions) -> list[tuple[str, str, ast.AST]]:
    """`(function, parameter, argument)` for each argument of `call` to a function of
    the same module of its name (a method called on any receiver, its `self` skipped)."""
    callee = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", None)
    bound = []
    for function in functions.get(callee, ()):
        parameters = [arg.arg for arg in (*function.args.posonlyargs, *function.args.args)]
        if isinstance(call.func, ast.Attribute) and parameters[:1] in (["self"], ["cls"]):
            parameters = parameters[1:]
        bound.extend((callee, parameter, arg) for parameter, arg in zip(parameters, call.args)
                     if not isinstance(arg, ast.Starred))
        bound.extend((callee, keyword.arg, keyword.value) for keyword in call.keywords if keyword.arg)
    return bound


def _aliases(scope, names) -> frozenset:
    """`names`, and each local name of `scope` assigned a value that holds one raw (an
    f-string, `%` or `.format` of it, or it): `why = f"…{exc}"` carries `exc`."""
    names = set(names)
    assigns = [node for node in ast.walk(scope) if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
               and node.value is not None]
    while True:
        more = {target.id for node in assigns if _raw(node.value, names)
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                if isinstance(target, ast.Name)} - names
        if not more:
            return frozenset(names)
        names |= more


def logged_parameters(functions) -> set[tuple[str, str]]:
    """The `(function, parameter)` pairs of a module whose value reaches a log call:
    passed to one raw, in a string built from it, or passed on to another such
    parameter (`_unacknowledged(why)` → `_relay_lost(f"{why}; …")` → `log.warning(…, why)`)."""
    logged: set[tuple[str, str]] = set()
    calls = {id(function): [node for node in ast.walk(function) if isinstance(node, ast.Call)]
             for defs in functions.values() for function in defs}
    while True:
        more = set()
        for name, defs in functions.items():
            for function in defs:
                arguments = function.args
                for parameter in (arg.arg for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)):
                    if parameter in ("self", "cls") or (name, parameter) in logged:
                        continue
                    names = _aliases(function, {parameter})
                    for call in calls[id(function)]:
                        if ((_is_log_call(call) and any(_raw(arg, names) for arg in call.args))
                                or any((callee, bound) in logged and _raw(arg, names)
                                       for callee, bound, arg in _bound(call, functions))):
                            more.add((name, parameter))
                            break
        if not more:
            return logged
        logged |= more


def raw_log_sites(source: str, path: str) -> list[str]:
    """Every log call that passes a caught exception (an `except … as` name, or a
    parameter named `exc` or typed as an exception) other than through a scrubber,
    its class, or its errno or exit status; and every call that passes one so, in a
    string built from it or not, to a parameter of the same module's function whose
    value reaches a log call (`logged_parameters`; review r2 of #233). A local a
    string built from one is assigned to carries it too."""
    tree = ast.parse(source)
    functions = _functions(tree)
    logged = logged_parameters(functions)
    found = []

    def visit(node, names):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            names = names | _exception_parameters(node)
            if names:
                names = _aliases(node, names)
        if isinstance(node, ast.ExceptHandler) and node.name:
            names = _aliases(node, names | {node.name})
        if isinstance(node, ast.Call) and names:
            if _is_log_call(node):
                found.extend(f"{path}:{node.lineno} passes {name} raw" for arg in node.args for name in _raw(arg, names))
            else:
                found.extend(f"{path}:{node.lineno} passes {name} raw to {callee}({parameter}), which is logged"
                             for callee, parameter, arg in _bound(node, functions)
                             if (callee, parameter) in logged for name in _raw(arg, names))
        for child in ast.iter_child_nodes(node):
            visit(child, names)
    visit(tree, frozenset())
    return found


def test_no_daemon_module_logs_a_caught_exception_raw():
    """The handler scrubs every line; each site scrubs its own exception too, so the
    rule rests on two layers (D-LOG1 decision 3)."""
    found = [site for path in sorted((REPO / "subfleet").rglob("*.py"))
             for site in raw_log_sites(path.read_text(), str(path.relative_to(REPO)))]
    assert found == []


#: A module whose `lost` logs its argument through `gone`, and whose `kept` never logs.
RELAYED = """\
class Runner:
    def gone(self, why):
        self.log.warning("relay failed: %s", why)

    def lost(self, why):
        self.gone(f"{why}; no answer")

    def kept(self, why):
        raise ValueError(why)

    def step(self):
        try:
            pass
        except Exception as exc:
"""
#: runner.py's two lines at 2b64f76a3: `{exc}` into `_unacknowledged`'s message,
#: which `_relay_lost` logs (review r2 of #233).
OLD_RUNNER_LINES = ('self._unacknowledged(f"relay status: {exc}")', 'self._unacknowledged(f"frame {frame.tag}: {exc}")')


@pytest.mark.parametrize("source,raw", [
    ("try:\n    pass\nexcept Exception as exc:\n    log.warning('x %s', exc)\n", True),
    ("try:\n    pass\nexcept Exception as exc:\n    self.log.error('x %s: %s', type(exc).__name__, exc)\n", True),
    ("try:\n    pass\nexcept Exception as error:\n    self.service.log.warning(f'x {error}')\n", True),
    ("def _failed(self, exc):\n    self.log.error('x %s', str(exc))\n", True),
    ("def f(self, frame, err: OSError):\n    self.log.warning('x %s', err)\n", True),
    ("def f(self, frame, exc: FrameTooLarge):\n    self.log.warning('x %s', exc)\n", True),
    ("hub = WaitHub(on_error=lambda exc: self.log.warning('x %s', exc))\n", True),
    ("try:\n    pass\nexcept Exception as exc:\n    log.warning('x %s', refusals.scrubbed_words(exc))\n", False),
    ("try:\n    pass\nexcept OSError as exc:\n    log.error('x %s %s', errno.errorcode.get(exc.errno, exc.errno), type(exc).__name__)\n", False),
    ("try:\n    pass\nexcept Exception as exc:\n    log.error('x', exc_info=exc)\n", False),
    ("try:\n    pass\nexcept Exception as exc:\n    raise ValueError(f'x {exc}')\n", False),
    # Review r2 of #233: into a string another function of the module logs.
    (RELAYED + "            self.lost(f'relay status: {exc}')\n", True),
    (RELAYED + "            self.lost('relay status: %s' % exc)\n", True),
    (RELAYED + "            self.lost('relay status: {}'.format(exc))\n", True),
    (RELAYED + "            self.lost(why=str(exc))\n", True),
    (RELAYED + "            why = f'relay status: {exc}'\n            self.lost(why)\n", True),
    (RELAYED + "            self.lost(f'relay status: {refusals.fixed_error(exc)}')\n", False),
    (RELAYED + "            self.kept(f'relay status: {exc}')\n", False),
    ("try:\n    pass\nexcept Exception as exc:\n    why = f'x {exc}'\n    log.warning('%s', why)\n", True),
])
def test_the_site_lint_tells_raw_from_scrubbed(source, raw):
    assert bool(raw_log_sites(source, "x.py")) is raw




def test_the_site_lint_fails_runner_py_s_old_lines():
    """C-25.7: each daemon module logs a caught exception only through
    `refusals.scrubbed_words`, its class or its errno. runner.py put `{exc}` into a
    message `_unacknowledged` passes to `_relay_lost`, which logs it; the lint read
    only log calls. It now follows a parameter to the log call it reaches: runner.py
    as it was fails it, at exactly those two lines."""
    source = (REPO / "subfleet" / "conversations" / "runner.py").read_text(encoding="utf-8")
    old = source
    for line in OLD_RUNNER_LINES:
        old = old.replace(line.replace("{exc}", "{refusals.fixed_error(exc)}"), line)
    numbers = [number for number, text in enumerate(old.splitlines(), 1) if any(line in text for line in OLD_RUNNER_LINES)]
    assert len(numbers) == 2
    assert sorted(raw_log_sites(old, "runner.py")) == sorted(
        f"runner.py:{number} passes exc raw to _unacknowledged(why), which is logged" for number in numbers)
    assert raw_log_sites(source, "runner.py") == []
