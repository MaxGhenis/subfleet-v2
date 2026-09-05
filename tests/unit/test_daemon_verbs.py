"""`daemon start|stop|status|logs|install` against a stub `subfleetd` (C-17.1).

Every test names the clause it proves (C-20.5).
"""

from __future__ import annotations

import contextlib
import json
import os
import plistlib
import signal
import sys
from pathlib import Path

import pytest

from subfleet import cli
from subfleet.client import Client

REPO = str(Path(__file__).resolve().parents[2])

STUB = '''#!{python}
"""A stand-in subfleetd: binds the socket, writes the lock, answers daemon.status."""
import json, os, signal, socket, sys
sys.path.insert(0, {repo!r})
from pathlib import Path
from subfleet import protocol
from subfleet.client import boot_id, proc_start

root = Path(sys.argv[sys.argv.index("--state-root") + 1])
sock_path, lock_path = root / "daemon.sock", root / "daemon.lock"
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(str(sock_path))
server.listen(8)
server.settimeout(0.2)
lock_path.write_text(json.dumps({{"pid": os.getpid(), "boot_id": boot_id(),
                                 "proc_start": proc_start(os.getpid()),
                                 "version": "stub"}}))
print("stub subfleetd listening", flush=True)


def stop(*_):
    sock_path.unlink(missing_ok=True)
    lock_path.unlink(missing_ok=True)
    os._exit(0)


signal.signal(signal.SIGTERM, stop)
while True:
    try:
        conn, _ = server.accept()
    except TimeoutError:
        continue
    with conn, conn.makefile("rb") as stream:
        line = stream.readline()
        if not line.strip():
            continue
        request = protocol.decode_request(line)
        conn.sendall(protocol.encode(protocol.ok(
            request.id, {{"version": "stub", "lanes": [], "readings": [],
                         "closures": [], "running": []}})))
'''

DEAD_STUB = '''#!{python}
import sys
print("stub refused to start: no credentials", file=sys.stderr, flush=True)
sys.exit(3)
'''


def _write_stub(path: Path, body: str) -> Path:
    path.write_text(body.format(python=sys.executable, repo=REPO))
    path.chmod(0o755)
    return path


@pytest.fixture
def stub(root, monkeypatch, tmp_path):
    """A `subfleetd` that really listens, torn down however the test ends."""
    path = _write_stub(tmp_path / "stub-subfleetd", STUB)
    monkeypatch.setenv("SUBFLEET_DAEMON_BIN", str(path))
    yield path
    info = Client(root).lock_info() or {}
    pid = info.get("pid")
    if isinstance(pid, int):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def test_daemon_start_waits_for_the_socket_then_status_and_stop(stub, root, capsys):
    """C-17.1, C-5.8 start launches subfleetd detached and waits for the socket."""
    assert cli.main(["daemon", "start"]) == 0
    captured = capsys.readouterr()
    assert (root / "daemon.sock").exists()
    lock = json.loads((root / "daemon.lock").read_text())
    assert captured.out.strip() == str(lock["pid"])
    assert "started" in captured.err

    assert cli.main(["daemon", "status"]) == 0
    captured = capsys.readouterr()
    assert "ping        ok" in captured.out
    assert str(lock["pid"]) in captured.out

    assert cli.main(["daemon", "start"]) == 0            # idempotent
    assert "already running" in capsys.readouterr().err

    assert cli.main(["daemon", "stop"]) == 0
    assert "stopped" in capsys.readouterr().err
    assert not (root / "daemon.sock").exists()


def test_daemon_start_runs_in_its_own_session(stub, root, capsys):
    """C-17.1 `daemon start` launches subfleetd detached in its own session.

    Leading its own session is what makes the daemon outlive the shell that
    started it, so the check is `getsid(pid) == pid`, not merely a different
    process group.
    """
    assert cli.main(["daemon", "start"]) == 0
    capsys.readouterr()
    pid = json.loads((root / "daemon.lock").read_text())["pid"]
    assert os.getsid(pid) == pid
    assert os.getsid(pid) != os.getsid(0)
    assert cli.main(["daemon", "stop"]) == 0
    capsys.readouterr()


def test_daemon_start_exits_69_with_the_log_tail(root, monkeypatch, tmp_path, capsys):
    """C-17.3 a daemon that never opens the socket is exit 69 with the log tail."""
    monkeypatch.setenv("SUBFLEET_DAEMON_BIN",
                       str(_write_stub(tmp_path / "dead-subfleetd", DEAD_STUB)))
    assert cli.main(["daemon", "start"]) == 69
    captured = capsys.readouterr()
    assert "did not appear within 10s" in captured.err
    assert "stub refused to start" in captured.err
    assert (root / "daemon.log").exists()


def test_daemon_status_without_a_daemon_is_69(root, capsys):
    """C-17.3, C-5.8 `daemon status` reports an absent socket and lock and exits 69."""
    assert cli.main(["daemon", "status"]) == 69
    captured = capsys.readouterr()
    assert "socket" in captured.out and "absent" in captured.out
    assert "lock        absent" in captured.out


def test_daemon_stop_on_a_stale_lock_is_a_no_op(root, capsys):
    """C-5.8 a lock whose recorded identity is dead is not a daemon to stop."""
    (root / "daemon.lock").write_text(json.dumps(
        {"pid": 999999, "boot_id": "1", "proc_start": "Mon Jan  1 00:00:00 2001"}))
    assert cli.main(["daemon", "stop"]) == 0
    assert "stale lock" in capsys.readouterr().err


def test_daemon_stop_refuses_an_identity_it_cannot_verify(root, capsys):
    """C-5.3 the CLI never signals a pid whose identity it could not confirm."""
    (root / "daemon.lock").write_text(json.dumps({"pid": os.getpid()}))
    assert cli.main(["daemon", "stop"]) == 1
    captured = capsys.readouterr()
    assert "cannot verify" in captured.err and "C-5.3" in captured.err


def test_daemon_stop_without_a_lock_is_a_no_op(root, capsys):
    """C-5.8 no lock file means no daemon."""
    assert cli.main(["daemon", "stop"]) == 0
    assert "not running" in capsys.readouterr().err


def test_daemon_logs_tails_the_log(root, capsys):
    """C-17.1 `daemon logs` prints the tail of daemon.log."""
    (root / "daemon.log").write_text("\n".join(f"line {n}" for n in range(100)) + "\n")
    assert cli.main(["daemon", "logs", "-n", "3"]) == 0
    assert capsys.readouterr().out.splitlines() == ["line 97", "line 98", "line 99"]
    (root / "daemon.log").unlink()
    assert cli.main(["daemon", "logs"]) == 1
    assert f"daemon logs: no {root / 'daemon.log'}" in capsys.readouterr().err


def test_daemon_install_dry_run_prints_the_plist(root, monkeypatch, tmp_path, capsys):
    """C-17.1 `daemon install --dry-run` prints the plist and writes nothing."""
    monkeypatch.setenv("SUBFLEET_DAEMON_BIN", str(tmp_path / "subfleetd"))
    target = tmp_path / "com.subfleet.daemon.plist"
    monkeypatch.setattr(cli, "PLIST_PATH", str(target))
    assert cli.main(["daemon", "install", "--dry-run"]) == 0
    captured = capsys.readouterr()
    plist = plistlib.loads(captured.out.encode())
    assert plist["Label"] == "com.subfleet.daemon"
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["ProgramArguments"] == [str(tmp_path / "subfleetd"),
                                         "--state-root", str(root)]
    assert plist["EnvironmentVariables"]["SUBFLEET_HOME"] == str(root)
    assert plist["StandardOutPath"] == str(root / "daemon.log")
    assert "would write" in captured.err
    assert not target.exists()          # --dry-run writes nothing and loads nothing


def test_the_state_root_comes_from_subfleet_home(root, monkeypatch):
    """C-2.1 the state root is $SUBFLEET_HOME, default ~/.subfleet/."""
    from subfleet.client import state_root
    assert state_root() == root
    monkeypatch.delenv("SUBFLEET_HOME")
    assert state_root() == Path("~/.subfleet").expanduser()


def test_doctor_reports_the_layout_and_a_stale_socket(root, capsys, monkeypatch):
    """C-17.1 `doctor` reports the state root layout and a socket the lock denies.

    The provider versions are stubbed so the test measures this lane's logic and
    not whether the machine running it happens to have claude and codex on PATH.
    """
    monkeypatch.setattr(cli, "_version", lambda binary: ("ok", f"stub {binary}"))
    assert cli.main(["doctor"]) == 0
    text = capsys.readouterr().out
    assert "state root layout" in text and "daemon.sock and daemon.lock agree" in text
    (root / "daemon.sock").touch()
    (root / "daemon.lock").write_text(json.dumps(
        {"pid": 999999, "boot_id": "1", "proc_start": "Mon Jan  1 00:00:00 2001"}))
    assert cli.main(["doctor"]) == 1
    assert "the lock is stale" in capsys.readouterr().out


def test_doctor_json_emits_one_object_per_check(root, capsys, monkeypatch):
    """C-17.4 `doctor --json` emits JSON objects only."""
    monkeypatch.setattr(cli, "_version", lambda binary: ("ok", f"stub {binary}"))
    assert cli.main(["doctor", "--json"]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    checks = [json.loads(line) for line in lines]
    names = {check["check"] for check in checks}
    assert {"claude --version", "codex --version", "uv --version",
            "state root layout", "daemon.sock and daemon.lock agree",
            "PATH shadows for claude", "PATH shadows for codex",
            "never-rules hook in ~/.claude/settings.json"} <= names
    assert all(check["status"] in {"ok", "warn", "fail"} for check in checks)


def test_doctor_live_runs_the_offline_checks_and_the_identity_comparison(root, capsys):
    """C-17.1, C-10.3 `doctor --live` adds the checks that need a credential.

    With no desktop login readable it says so rather than failing: an unverified
    desktop identity is a warning, and lanes fall back to matching its label.
    """
    assert cli.main(["doctor", "--live"]) == 0
    printed = capsys.readouterr().out
    assert "cached ~/.claude.json agrees with the desktop credential" in printed
    assert "claude --version" in printed        # the offline checks still run


def test_doctor_names_a_missing_never_rules_hook(root, capsys, monkeypatch):
    """C-14.3 doctor reports whether ~/.claude/settings.json carries the hook."""
    monkeypatch.setattr(cli, "_version", lambda binary: ("ok", f"stub {binary}"))
    settings = root / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": []}}))
    rows = {check["check"]: check
            for check in cli.doctor_checks(root, claude_settings=settings)}
    hook = rows["never-rules hook in ~/.claude/settings.json"]
    assert hook["status"] == "warn" and "C-14.3" in hook["detail"]
    settings.write_text(json.dumps(
        {"hooks": {"PreToolUse": [{"command": "guard-never-rules.sh"}]}}))
    rows = {check["check"]: check
            for check in cli.doctor_checks(root, claude_settings=settings)}
    assert rows["never-rules hook in ~/.claude/settings.json"]["status"] == "ok"
    settings.unlink()
    rows = {check["check"]: check
            for check in cli.doctor_checks(root, claude_settings=settings)}
    assert rows["never-rules hook in ~/.claude/settings.json"]["status"] == "warn"
    capsys.readouterr()


def test_daemon_start_waits_out_a_self_daemonising_subfleetd(root, monkeypatch,
                                                             tmp_path, capsys):
    """C-17.1 a subfleetd that double-forks exits 0; that is not a failure."""
    forking = tmp_path / "forking-subfleetd"
    forking.write_text(STUB.format(python=sys.executable, repo=REPO).replace(
        'root = Path(sys.argv[sys.argv.index("--state-root") + 1])',
        'if os.fork():\n    os._exit(0)\n'
        'os.setsid()\n'
        'root = Path(sys.argv[sys.argv.index("--state-root") + 1])'))
    forking.chmod(0o755)
    monkeypatch.setenv("SUBFLEET_DAEMON_BIN", str(forking))
    try:
        assert cli.main(["daemon", "start"]) == 0
        assert (root / "daemon.sock").exists()
    finally:
        capsys.readouterr()
        info = Client(root).lock_info() or {}
        if isinstance(info.get("pid"), int):
            with contextlib.suppress(OSError):
                os.kill(info["pid"], signal.SIGKILL)


def test_the_daemon_does_not_inherit_api_keys_or_a_session(root, monkeypatch):
    """C-14.4 the daemon outlives the shell, so it starts from a scrubbed env."""
    for name in ("ANTHROPIC_API_KEY", "CODEX_API_KEY", "OPENAI_API_KEY",
                 "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.setenv(name, "leaked")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = cli.daemon_env(root)
    assert not {name for name in env if name in cli.STRIPPED_ENV}
    assert env["SUBFLEET_HOME"] == str(root)
    assert env["PATH"] == "/usr/bin:/bin"


def test_daemon_stop_verifies_and_signals_one_snapshot(root, monkeypatch, capsys):
    """C-5.4 the lock is read once, so a new daemon's identity cannot vouch for
    an old daemon's pid."""
    seen: list[int] = []
    (root / "daemon.lock").write_text(json.dumps(
        {"pid": 4242, "boot_id": "b", "proc_start": "recorded"}))
    monkeypatch.setattr("subfleet.cli.same_process",
                        lambda pid, boot, start: seen.append(pid) or (
                            True if len(seen) == 1 else False))
    monkeypatch.setattr(os, "kill", lambda pid, sig: seen.append(-pid))
    assert cli.main(["daemon", "stop"]) == 0
    assert seen[0] == 4242 and -4242 in seen
    assert "stopped" in capsys.readouterr().err


def test_daemon_stop_refuses_a_lock_with_no_usable_pid(root, capsys):
    """C-5.8 a lock the CLI cannot read a pid out of is not something to signal."""
    (root / "daemon.lock").write_text(json.dumps({"pid": "not a number"}))
    assert cli.main(["daemon", "stop"]) == 1
    assert "no usable pid" in capsys.readouterr().err


def test_daemon_logs_line_counts(root, capsys):
    """C-17.3 `-n 0` prints nothing and a negative count is invalid input."""
    (root / "daemon.log").write_text("a\nb\nc\n")
    assert cli.main(["daemon", "logs", "-n", "0"]) == 0
    assert capsys.readouterr().out == ""
    assert cli.main(["daemon", "logs", "-n", "-1"]) == 2
    assert "non-negative" in capsys.readouterr().err


def test_doctor_names_a_state_root_whose_socket_cannot_exist(capsys, monkeypatch,
                                                             tmp_path):
    """C-2.1 a SUBFLEET_HOME too deep for AF_UNIX can never hold a daemon."""
    deep = tmp_path / ("d" * 120)
    deep.mkdir()
    monkeypatch.setattr(cli, "_version", lambda binary: ("ok", f"stub {binary}"))
    rows = {check["check"]: check for check in cli.doctor_checks(deep)}
    row = rows["socket path fits AF_UNIX"]
    assert row["status"] == "fail" and "shorter path" in row["detail"]
    capsys.readouterr()
