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
    if sys.version_info >= (3, 14):          # and each thread's name (review r1 of #233, P3)
        import threading
        assert f"[{threading.current_thread().name}]" in text


#: A name a thread is given: fixed words (a pool's prefix too, to which the pool adds
#: `_<n>`). Without one, Python names it `Thread-<n> (<function>)`.
FIXED_THREAD_NAME = re.compile(r"[a-z][a-z0-9-]*\Z")
#: The one computed name: a turn runner's, its attempt's id (C-1.2).
COMPUTED_THREAD_NAMES = {"f'turn:{self.attempt_id}'"}
#: That id: the job of a turn is named `turn-<conversation id>` (C-26.12), so ids only.
TURN_THREAD_NAME = re.compile(r"turn:\d{8}-\d{6}-turn-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?:-\d+)?/a\d+\Z")


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


# --- the call sites themselves ------------------------------------------------------

LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical", "log"})
#: What may carry a caught exception into a log call: the scrubbers, its class, its
#: errno or exit status.
SCRUBBERS = frozenset({"refusals.scrubbed_words", "refusals.scrubbed_line", "refusals.fixed_error",
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


def raw_log_sites(source: str, path: str) -> list[str]:
    """Every log call that passes a caught exception (an `except … as` name, or a
    parameter named `exc` or typed as an exception) other than through a scrubber,
    its class, or its errno or exit status."""
    found = []

    def visit(node, names):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            names = names | _exception_parameters(node)
        if isinstance(node, ast.ExceptHandler) and node.name:
            names = names | {node.name}
        if isinstance(node, ast.Call) and _is_log_call(node):
            found.extend(f"{path}:{node.lineno} passes {name} raw" for arg in node.args for name in _raw(arg, names))
        for child in ast.iter_child_nodes(node):
            visit(child, names)
    visit(ast.parse(source), frozenset())
    return found


def test_no_daemon_module_logs_a_caught_exception_raw():
    """The handler scrubs every line; each site scrubs its own exception too, so the
    rule rests on two layers (D-LOG1 decision 3)."""
    found = [site for path in sorted((REPO / "subfleet").rglob("*.py"))
             for site in raw_log_sites(path.read_text(), str(path.relative_to(REPO)))]
    assert found == []


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
])
def test_the_site_lint_tells_raw_from_scrubbed(source, raw):
    assert bool(raw_log_sites(source, "x.py")) is raw
