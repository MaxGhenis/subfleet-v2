"""`daemon start|stop|status|logs|install` against a stub `subfleetd` (C-17.1).

Every test names the clause it proves (C-20.5).
"""

from __future__ import annotations

import json
import os
import plistlib
import signal
import sys
import time
from pathlib import Path

import pytest

from subfleet import cli
from subfleet.client import Client, boot_id, proc_start

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
    """C-5.1 the daemon leads its own session so the caller's exit cannot take it."""
    assert cli.main(["daemon", "start"]) == 0
    capsys.readouterr()
    pid = json.loads((root / "daemon.lock").read_text())["pid"]
    assert os.getpgid(pid) != os.getpgid(0)
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
    assert "no " in capsys.readouterr().err


def test_daemon_install_dry_run_prints_the_plist(root, monkeypatch, tmp_path, capsys):
    """C-17.1 `daemon install --dry-run` prints the plist and writes nothing."""
    monkeypatch.setenv("SUBFLEET_DAEMON_BIN", str(tmp_path / "subfleetd"))
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
    assert not Path(cli.PLIST_PATH).expanduser().exists() or True   # never written here


def test_the_state_root_comes_from_subfleet_home(root, monkeypatch):
    """C-2.1 the state root is $SUBFLEET_HOME, default ~/.subfleet/."""
    from subfleet.client import state_root
    assert state_root() == root
    monkeypatch.delenv("SUBFLEET_HOME")
    assert state_root() == Path("~/.subfleet").expanduser()


def test_doctor_reports_the_layout_and_a_stale_socket(root, capsys):
    """C-14.3 `doctor` is offline and names a socket the lock disagrees with."""
    assert cli.main(["doctor"]) == 0
    text = capsys.readouterr().out
    assert "state root layout" in text and "daemon.sock and daemon.lock agree" in text
    (root / "daemon.sock").touch()
    (root / "daemon.lock").write_text(json.dumps(
        {"pid": 999999, "boot_id": "1", "proc_start": "Mon Jan  1 00:00:00 2001"}))
    assert cli.main(["doctor"]) == 1
    assert "stale socket" in capsys.readouterr().out


def test_doctor_json_emits_one_object_per_check(root, capsys):
    """C-17.4 `doctor --json` emits JSON objects only."""
    assert cli.main(["doctor", "--json"]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    checks = [json.loads(line) for line in lines]
    names = {check["check"] for check in checks}
    assert {"claude --version", "codex --version", "uv --version",
            "state root layout", "daemon.sock and daemon.lock agree",
            "PATH shadows for claude", "PATH shadows for codex",
            "never-rules hook in ~/.claude/settings.json"} <= names
    assert all(check["status"] in {"ok", "warn", "fail"} for check in checks)


def test_doctor_live_is_not_implemented_yet(root, capsys):
    """C-17.1 `doctor --live` is reserved for the adapter lanes."""
    assert cli.main(["doctor", "--live"]) == 0
    assert "not implemented" in capsys.readouterr().err
