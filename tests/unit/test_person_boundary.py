"""D-PB1: a controlling terminal is not a person (C-25.6, design D-8).

Before 2026-10-11 `peers.judge` accepted any caller with a controlling terminal
once the guardian and marker vetoes passed. A process an agent started could
drop its markers, hold a pseudo-terminal and outlive its parent; adopted by
launchd, nothing above it was Subfleet's, and it answered approvals, steered,
resolved and unblocked as the person. These tests drive the real `judge`, and
the real service ops that call it, through the process-inspection fakes
(`chain`, `executable`); no process with a terminal is started.
"""

from __future__ import annotations

import functools
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

import hypothesis
import pytest
from hypothesis import strategies as st

from subfleet.conversations import peers
from subfleet.conversations import service as service_module
from subfleet.conversations.peers import APP_EXECUTABLES, Proc, judge
from subfleet.conversations.store import ConversationError
from tests.unit.test_conversation_service import SETTINGS, conversation, svc  # noqa: F401 (a fixture)

APP = APP_EXECUTABLES[0]
DEV = "/Users/m/build/Subfleet Dev.app/Contents/MacOS/Subfleet Dev"
ROOT = "/Users/m/.subfleet-dev"
SHELL = "/bin/zsh"


def chain_of(*procs):
    return lambda pid: list(procs)


def exe(path):
    return lambda pid: path


# --- the shapes D-PB1 names -------------------------------------------------------

#: An agent's process, markers dropped, adopted by launchd, holding a pseudo-terminal.
ORPHAN = (Proc(500, 1, "ttys009", "/usr/bin/python3 relay.py HOME=/Users/m PATH=/usr/bin"),
          Proc(1, 0, "??", "/sbin/launchd"))
#: A child the orphan forked, with a terminal of its own: its parent is not launchd.
ORPHANS_CHILD = (Proc(501, 500, "ttys010", "/bin/zsh -i HOME=/Users/m"), *ORPHAN)
#: An SSH login (Tailscale SSH or sshd): a terminal and no Subfleet ancestry.
SSH_LOGIN = (Proc(700, 699, "ttys004", "-zsh HOME=/Users/m SSH_CONNECTION=100.64.0.2 52000 100.64.0.1 22"),
             Proc(699, 698, "ttys004", "sshd-session: m@ttys004"),
             Proc(698, 1, "??", "/usr/libexec/sshd-session"),
             Proc(1, 0, "??", "/sbin/launchd"))
#: A person's Terminal window: also a terminal and no Subfleet ancestry.
TERMINAL_WINDOW = (Proc(800, 799, "ttys003", "-zsh HOME=/Users/m"),
                   Proc(799, 798, "ttys003", "login -pf m"),
                   Proc(798, 1, "??", "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"),
                   Proc(1, 0, "??", "/sbin/launchd"))

REFUSED_TERMINALS = {"orphan": ORPHAN, "orphans-child": ORPHANS_CHILD, "ssh-login": SSH_LOGIN,
                     "terminal-window": TERMINAL_WINDOW}


@pytest.mark.parametrize("shape", sorted(REFUSED_TERMINALS))
def test_a_terminal_without_subfleet_ancestry_is_not_a_person(shape):
    """D-PB1: an orphan holding a pseudo-terminal, the terminal child it forks, an SSH
    login and a person's Terminal window are all refused; a terminal admits nobody."""
    procs = REFUSED_TERMINALS[shape]
    verdict = judge(procs[0].pid, chain=chain_of(*procs), executable=exe(SHELL), root=ROOT)
    assert not verdict.person, verdict
    assert verdict.reason.startswith("this needs the Subfleet app"), verdict
    assert f"a terminal ({procs[0].tty})" in verdict.reason, verdict
    assert verdict.pid == procs[0].pid


def test_a_caller_with_no_terminal_that_is_not_the_app_is_told_it_needs_the_app():
    verdict = judge(10, chain=chain_of(Proc(10, 1, "??", "/usr/bin/python3 x.py")), executable=exe("/usr/bin/python3"))
    assert not verdict.person and verdict.reason == "this needs the Subfleet app"


@pytest.mark.parametrize("shape", sorted(REFUSED_TERMINALS))
def test_the_app_is_still_a_person_whatever_its_ancestry_and_terminal(shape):
    """C-25.6: the installed app's executable is accepted; so is the development build
    the daemon was told of (D-21). Neither needs, or is refused for, a terminal."""
    procs = REFUSED_TERMINALS[shape]
    as_app = (Proc(procs[0].pid, procs[0].ppid, procs[0].tty, f"{APP} HOME=/Users/m"), *procs[1:])
    assert judge(as_app[0].pid, chain=chain_of(*as_app), executable=exe(APP), root=ROOT).person
    detached = (Proc(10, 1, "??", f"{APP} HOME=/Users/m"), Proc(1, 0, "??", "/sbin/launchd"))
    verdict = judge(10, chain=chain_of(*detached), executable=exe(APP), root=ROOT)
    assert verdict.person and verdict.reason == "the Subfleet app"
    dev = (Proc(10, 1, "??", f"{DEV} HOME=/Users/m"), Proc(1, 0, "??", "/sbin/launchd"))
    assert judge(10, chain=chain_of(*dev), executable=exe(DEV), app_executables=(APP, DEV), root=ROOT).person
    assert not judge(10, chain=chain_of(*dev), executable=exe(DEV), root=ROOT).person


VETOES = {
    "guardian": ("/v/python -m subfleet.guardian --attempt-dir /x -- claude -p", "the caller runs under a Subfleet guardian"),
    "attempt": ("bash SUBFLEET_ATTEMPT=j/a1 PATH=/bin", "the caller carries Subfleet's attempt markers"),
    "job": ("bash SUBFLEET_JOB=j PATH=/bin", "the caller carries Subfleet's attempt markers"),
    "root": (f"bash SUBFLEET_ROOT={ROOT} PATH=/bin", "the caller carries Subfleet's attempt markers"),
}


@pytest.mark.parametrize("veto", sorted(VETOES))
@pytest.mark.parametrize("where", ["caller", "parent", "top"])
@pytest.mark.parametrize("tty", ["??", "ttys001"])
def test_the_marker_and_guardian_vetoes_are_unchanged_and_bind_the_app_too(veto, where, tty):
    """C-25.6: a guardian's descendant, or a process carrying the markers anywhere in its
    ancestry, is refused, even when the caller is the app's executable."""
    command, reason = VETOES[veto]
    procs = [Proc(10, 9, tty, f"{APP} HOME=/Users/m"), Proc(9, 8, tty, "/bin/zsh"), Proc(8, 1, "??", "login")]
    index = {"caller": 0, "parent": 1, "top": 2}[where]
    procs[index] = Proc(procs[index].pid, procs[index].ppid, procs[index].tty, f"{procs[index].command} {command}")
    verdict = judge(10, chain=chain_of(*procs), executable=exe(APP), root=ROOT)
    assert not verdict.person and verdict.reason == reason, verdict


def test_another_state_roots_marker_is_not_a_veto():
    """C-25.6: `SUBFLEET_ROOT` vetoes only when it names this daemon's state root."""
    procs = (Proc(10, 9, "??", f"{APP} HOME=/Users/m"), Proc(9, 1, "??", "zsh SUBFLEET_ROOT=/Users/m/other"))
    assert judge(10, chain=chain_of(*procs), executable=exe(APP), root=ROOT).person


def test_an_unidentified_or_uninspectable_caller_is_refused():
    assert not judge(None, executable=exe(APP)).person
    assert not judge(10, chain=lambda pid: [], executable=exe(APP)).person
    # The kernel could not say what the caller runs: never the app.
    assert not judge(10, chain=chain_of(Proc(10, 1, "ttys001", "zsh")), executable=exe(None)).person


# --- every person-only op, through the service ---------------------------------------


def person_only_calls(cid: str) -> dict[str, tuple[str, dict]]:
    """Each C-25.6 operation, with arguments that reach its person check."""
    mid = str(uuid.uuid4())
    widened = {**SETTINGS, "permission": "bypass"}
    return {
        "approval.get": ("approval.get", {"approval_id": "a-1"}),
        "approval.respond": ("approval.respond", {"approval_id": "a-1", "decision": "allow", "nonce": "n",
                                                  "request_sha256": "0" * 64}),
        "message.steer": ("message.steer", {"message_id": mid}),
        "message.resolve": ("message.resolve", {"message_id": mid, "resolution": "delivered", "confirm": True}),
        "conversation.unblock": ("conversation.unblock", {"conversation_id": cid, "choice": "leave", "confirm": True}),
        "conversation.settings widen": ("conversation.settings", {"conversation_id": cid, "settings": widened,
                                                                  "confirm_widen": True}),
        "conversation.settings allow_main": ("conversation.settings", {"conversation_id": cid, "allow_main": True}),
        "conversation.create above Ask": ("conversation.create", {"provider": "claude", "request_id": "c-1",
                                                                  "settings": widened, "confirm_widen": True}),
        "conversation.create allow_main": ("conversation.create", {"provider": "claude", "request_id": "c-2",
                                                                   "settings": SETTINGS, "allow_main": True}),
        "conversation.handoff above Ask": ("conversation.handoff", {
            "request_id": "h-1", "from": {"conversation_id": cid},
            "to": {"provider": "claude", "settings": widened}, "confirm_widen": True}),
        "conversation.handoff allow_main": ("conversation.handoff", {
            "request_id": "h-2", "from": {"conversation_id": cid},
            "to": {"provider": "claude", "settings": SETTINGS, "allow_main": True}}),
    }


PERSON_ONLY = sorted(person_only_calls("c").keys())


def with_process_table(monkeypatch, procs, path):
    """The service's own `judge`, reading this process table instead of `ps`."""
    monkeypatch.setattr(service_module, "judge",
                        functools.partial(peers.judge, chain=chain_of(*procs), executable=exe(path)))


@pytest.mark.parametrize("shape", sorted(REFUSED_TERMINALS))
@pytest.mark.parametrize("name", PERSON_ONLY)
def test_every_person_only_op_refuses_a_terminal_caller_with_exit_7_naming_the_app(svc, monkeypatch, shape, name):
    """D-PB1: approve, read an approval, steer, resolve, unblock, widen, allow main, and
    create or hand off above Ask or on main, each refused (exit 7) for a terminal."""
    procs = REFUSED_TERMINALS[shape]
    with_process_table(monkeypatch, procs, SHELL)
    cid = conversation(svc)
    op, args = person_only_calls(cid)[name]
    with pytest.raises(ConversationError) as refused:
        svc.handle(op, args, procs[0].pid)
    error = refused.value
    assert (error.reason, error.code) == ("person-only", 7), error
    assert "this needs the Subfleet app" in str(error) and error.fix == "answer it in the Subfleet app"


@pytest.mark.parametrize("name", PERSON_ONLY)
def test_every_person_only_op_lets_the_app_past_its_person_check(svc, monkeypatch, name):
    """C-25.6: the same calls from the app's executable get past the person check; what
    they do next (a missing approval, a message that is not there) is their own answer."""
    with_process_table(monkeypatch, (Proc(10, 1, "??", f"{APP} HOME=/Users/m"),), APP)
    cid = conversation(svc)
    op, args = person_only_calls(cid)[name]
    try:
        svc.handle(op, args, 10)
    except ConversationError as error:
        assert error.reason != "person-only", error
    except (KeyError, LookupError):
        pass                                    # past the check: the approval or message is not there


def test_the_development_build_is_the_app_only_on_a_development_state_root(svc, monkeypatch, tmp_path):
    """D-21 with D-PB1: on a development root, `SUBFLEET_DEV_APP_EXECUTABLE` is the app;
    on ~/.subfleet it is not, and a terminal is not either."""
    monkeypatch.setenv("SUBFLEET_DEV_APP_EXECUTABLE", DEV)
    with_process_table(monkeypatch, (Proc(10, 1, "??", f"{DEV} HOME=/Users/m"),), DEV)
    assert svc._person(10, "answering an approval").reason == "the Subfleet app"
    monkeypatch.setenv("HOME", str(tmp_path))
    svc.root = tmp_path / ".subfleet"
    svc.root.mkdir(exist_ok=True)
    with pytest.raises(ConversationError) as refused:
        svc._person(10, "answering an approval")
    assert refused.value.code == 7


# --- property: only the app, and never under a veto ---------------------------------------

TERMINALS = st.sampled_from(["??", "-", "", "ttys000", "ttys009", "ttys042", "console", "pts/3"])
CALLERS = st.sampled_from([APP, DEV, SHELL, "/usr/bin/python3", "/usr/libexec/sshd-session",
                           "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal",
                           f"{APP}2", "/Applications/Subfleet.app/Contents/MacOS/Subfleet Helper",
                           "/tmp/Subfleet", None])
ROOTS = st.sampled_from([None, ROOT, "/Users/m/.subfleet"])
FRAGMENTS = st.sampled_from([
    "", "HOME=/Users/m", "PATH=/usr/bin:/bin", "TERM=xterm-256color", "SSH_TTY=/dev/ttys004",
    "subfleet.guardian", "SUBFLEET_ATTEMPT=j/a1", "SUBFLEET_JOB=j", f"SUBFLEET_ROOT={ROOT}",
    "SUBFLEET_ROOT=/Users/m/.subfleet", "SUBFLEET_ROOT=/Users/m/other", "SUBFLEET_HOME=/Users/m/.subfleet",
    "SUBFLEET_SESSION_ID=s", "CLAUDE_CODE_SESSION_ID=s", "subfleet run --task build",
])


@st.composite
def ancestries(draw):
    """A caller and up to seven ancestors: pids distinct, each the parent of the one
    before, ending at launchd or at a pid the table no longer has."""
    length = draw(st.integers(min_value=1, max_value=8))
    pids = draw(st.lists(st.integers(min_value=2, max_value=99_999), min_size=length, max_size=length, unique=True))
    ends_at_launchd = draw(st.booleans())
    procs = []
    for i, pid in enumerate(pids):
        last = i == len(pids) - 1
        ppid = pids[i + 1] if not last else (1 if ends_at_launchd else draw(st.integers(min_value=2, max_value=99_999)))
        words = draw(st.lists(FRAGMENTS, max_size=4))
        procs.append(Proc(pid, ppid, draw(TERMINALS), " ".join(["/bin/proc", *words]).strip()))
    if ends_at_launchd:
        procs.append(Proc(1, 0, "??", "/sbin/launchd"))
    return procs


def vetoed(procs, root) -> bool:
    for proc in procs:
        if "subfleet.guardian" in proc.command or "SUBFLEET_ATTEMPT=" in proc.command or "SUBFLEET_JOB=" in proc.command:
            return True
        if root and f"SUBFLEET_ROOT={root}" in proc.command:
            return True
    return False


@hypothesis.settings(max_examples=2000, deadline=None)
@hypothesis.given(procs=ancestries(), caller=CALLERS, root=ROOTS, dev_known=st.booleans())
def test_property_a_caller_is_a_person_only_if_it_is_the_app_and_nothing_above_it_is_subfleets(
        procs, caller, root, dev_known):
    """C-25.6 after D-PB1, for any ancestry chain and any terminals: accepted if and only
    if the caller's image is the installed app (or the development build the daemon
    was told of) and no process in the chain is a guardian or carries the markers."""
    apps = (APP, DEV) if dev_known else (APP,)
    verdict = judge(procs[0].pid, chain=chain_of(*procs), app_executables=apps, root=root, executable=exe(caller))
    is_app = caller == APP or (dev_known and caller == DEV)
    assert verdict.person == (is_app and not vetoed(procs, root)), (verdict, procs, caller, root)
    assert verdict.pid == procs[0].pid


@hypothesis.settings(max_examples=1000, deadline=None)
@hypothesis.given(procs=ancestries(), caller=CALLERS, root=ROOTS, ttys=st.lists(TERMINALS, min_size=9, max_size=9))
def test_property_terminals_never_change_the_verdict(procs, caller, root, ttys):
    """D-PB1: giving any process in the chain a terminal, or taking one away, never
    changes whether the caller is a person."""
    retty = [Proc(p.pid, p.ppid, ttys[i], p.command) for i, p in enumerate(procs)]
    a = judge(procs[0].pid, chain=chain_of(*procs), root=root, executable=exe(caller))
    b = judge(retty[0].pid, chain=chain_of(*retty), root=root, executable=exe(caller))
    assert a.person == b.person


@hypothesis.settings(max_examples=1000, deadline=None)
@hypothesis.given(procs=ancestries(), caller=CALLERS, root=ROOTS, at=st.integers(min_value=0, max_value=8),
                  mark=st.sampled_from(["subfleet.guardian", "SUBFLEET_ATTEMPT=x", "SUBFLEET_JOB=x"]))
def test_property_a_veto_anywhere_in_the_chain_refuses(procs, caller, root, at, mark):
    """C-25.6: a guardian or marker on any one process of the chain refuses the caller,
    whoever it is."""
    at %= len(procs)
    marked = list(procs)
    marked[at] = Proc(procs[at].pid, procs[at].ppid, procs[at].tty, f"{procs[at].command} {mark}")
    assert not judge(marked[0].pid, chain=chain_of(*marked), root=root, executable=exe(caller),
                     app_executables=(APP, DEV)).person


@pytest.mark.skipif(sys.platform != "darwin", reason="LOCAL_PEERPID and proc_pidpath are macOS")
def test_the_end_to_end_tests_development_app_is_the_app_to_the_real_peer_check(tmp_path):
    """The e2e person (`tests/e2e/dev_app.py`, D-21) is recognised by the kernel's path for
    its image, read through the socket's real `LOCAL_PEERPID`; a Python client on the
    same socket is not the app. The ancestry is faked marker-free, because inside a
    Subfleet lane the real one carries the lane's markers (and the e2e person tests
    skip there), so this is the part of the e2e path a lane can check."""
    from tests.e2e import dev_app
    app = dev_app.build(tmp_path / "dev")
    if app is None:
        pytest.skip("no C compiler")
    sock_dir = Path(tempfile.mkdtemp(prefix="sf-pb1-", dir="/tmp"))
    path = sock_dir / "s"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    verdicts = []

    def serve(count):
        for _ in range(count):
            conn, _ = server.accept()
            with conn, conn.makefile("rb") as stream:
                stream.readline()
                pid = peers.peer_pid(conn)
                verdict = judge(pid, chain=lambda p: [Proc(p, 1, "??", "caller HOME=/Users/m")],
                                app_executables=(str(app),))
                verdicts.append((verdict, peers.executable_path(pid) if pid else None))
                conn.sendall((json.dumps({"ok": verdict.person, "reason": verdict.reason}) + "\n").encode())

    try:
        server.bind(str(path))
        server.listen(2)
        thread = threading.Thread(target=serve, args=(2,), daemon=True)
        thread.start()
        as_app = dev_app.request(app, path, {"op": "approval.get"}, timeout=30)
        python = subprocess.run([sys.executable, "-c", (
            "import socket,sys; c=socket.socket(socket.AF_UNIX); c.connect(sys.argv[1]); c.sendall(b'{}\\n');"
            "sys.stdout.write(c.makefile('rb').readline().decode())"), str(path)],
            capture_output=True, text=True, timeout=30, start_new_session=True, stdin=subprocess.DEVNULL)
        thread.join(30)
    finally:
        server.close()
        shutil.rmtree(sock_dir, ignore_errors=True)
    assert as_app == {"ok": True, "reason": "the Subfleet app"}, (as_app, verdicts)
    assert json.loads(python.stdout) == {"ok": False, "reason": "this needs the Subfleet app"}, python
    assert verdicts[0][1] == str(app) and " " in verdicts[0][1]      # the kernel's path, spaces kept


def test_the_module_says_a_terminal_is_not_a_person():
    """The boundary's own description matches what it does (C-25.6 after D-PB1)."""
    doc = " ".join((peers.__doc__ or "").split())
    assert "A controlling terminal is not a person (D-PB1" in doc
    assert "or a process with a controlling terminal" not in doc
