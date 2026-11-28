"""C-17.10: remote records and status through the CLI and daemon handlers."""

from contextlib import closing
from datetime import datetime, timezone
import json
import re
import socket
import subprocess
import threading

import pytest

from subfleet import cli, daemon as daemon_module, offline, protocol
from subfleet.client import DaemonError, DaemonUnavailable
from subfleet.contracts import Exit
from subfleet.store import NOTICE_WAKE_MIGRATION, Store
from tests.unit.test_protocol_secrecy import Connection

CLAUDE_URL = "https://claude.ai/code/session_Fake9"
CODEX_URL = "https://chatgpt.com/codex/tasks/task_Fake_3"
PRIVATE = "private@example.invalid"
SYNTHETIC_TOKEN = "ghp_" + "a" * 36
NOW = "2026-11-28T02:00:00Z"


@pytest.fixture
def service(tmp_path, monkeypatch):
    root = tmp_path / "state"
    monkeypatch.setenv("SUBFLEET_HOME", str(root))
    monkeypatch.setattr(daemon_module.procs, "boot_id", lambda: "remote-test-boot")
    monkeypatch.setattr(daemon_module.procs, "proc_start", lambda pid: "remote-test-start")

    with closing(daemon_module.Daemon(root, desktop_prober=lambda: None)) as daemon:
        class LocalClient:
            def call(self, op, args):
                conn = Connection()
                lock = threading.Lock()
                line = protocol.encode(protocol.Request(op=op, args=args, id="remote-test"))
                request = daemon._decode(conn, lock, line)
                if request is not None:
                    daemon._answer(conn, lock, request, None, lambda: False)
                response = protocol.decode_response(conn.sent)
                if response.error is not None:
                    raise DaemonError(response.error.code, response.error.message, response.error.fix)
                return response.result

        def connect(args):
            assert cli._root(args) == root
            return LocalClient()

        monkeypatch.setattr(cli, "_client", connect)
        yield daemon


def capture(capsys, *args, code=Exit.OK):
    result = cli.main(list(args))
    seen = capsys.readouterr()
    assert result == code, seen.err
    return seen


def events(store):
    return store.query("SELECT * FROM events ORDER BY event_id")


def event_data(remote_id, provider, provider_ref):
    return {"remote_id": remote_id, "provider": provider, "provider_ref": provider_ref}


def adopted_id(capsys, url, *options):
    seen = capture(capsys, "remote", "adopt", url, *options)
    assert not seen.err
    remote_id = seen.out.strip()
    assert re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-z0-9-]{1,40}", remote_id)
    return remote_id


def test_cli_remote_records_and_events_are_local_and_exclude_url_queries(service, capsys, monkeypatch):
    """Adopt, list and forget only local records, with one event per mutation."""
    before = events(service.store)

    def unexpected_io(*args, **kwargs):
        pytest.fail("remote adopt attempted a connection or command")

    with monkeypatch.context() as guard:
        guard.setattr(socket.socket, "connect", unexpected_io)
        guard.setattr(socket.socket, "connect_ex", unexpected_io)
        guard.setattr(subprocess, "Popen", unexpected_io)
        claude = adopted_id(capsys, f"{CLAUDE_URL}?email={PRIVATE}&token=secret-marker",
                            "--repo", "owner/repo", "--branch", "example/branch",
                            "--name", "example task", "--account", "claude-1")
        codex = adopted_id(capsys, CODEX_URL)
    decoded = json.loads(capture(capsys, "remote", "list", "--json").out)
    by_id = {row["remote_id"]: row for row in decoded["remote_jobs"]}
    assert by_id == {row["remote_id"]: row for row in service.store.query("SELECT * FROM remote_jobs")}
    assert set(by_id) == {claude, codex}
    assert by_id[claude] == {
        **event_data(claude, "claude-cloud", "session_Fake9"),
        "url": CLAUDE_URL, "repo": "owner/repo", "branch": "example/branch",
        "name": "example task", "account": "claude-1", "state": "running",
        "created_at": by_id[claude]["created_at"], "updated_at": by_id[claude]["created_at"],
        "last_polled_at": None,
    }
    assert datetime.fromisoformat(by_id[claude]["created_at"]).tzinfo is not None
    assert by_id[codex]["provider"] == "codex-cloud"
    assert by_id[codex]["provider_ref"] == "task_Fake_3"
    assert all(by_id[codex][field] is None for field in ("repo", "branch", "name", "account"))
    listed = capture(capsys, "remote", "list").out
    assert all(value in listed for value in (claude, CLAUDE_URL, codex, CODEX_URL))
    assert "@" not in json.dumps(by_id)
    assert "secret-marker" not in json.dumps(by_id)

    capture(capsys, "remote", "forget", claude)
    assert json.loads(capture(capsys, "remote", "list", "--json").out) == {"remote_jobs": [by_id[codex]]}
    assert not service.store.list_jobs()
    new = events(service.store)[len(before):]
    assert [(event["kind"], json.loads(event["data_json"])) for event in new] == [
        ("remote.adopted", event_data(claude, "claude-cloud", "session_Fake9")),
        ("remote.adopted", event_data(codex, "codex-cloud", "task_Fake_3")),
        ("remote.forgotten", event_data(claude, "claude-cloud", "session_Fake9")),
    ]
    assert all(event["job_id"] is None for event in new)
    assert "@" not in json.dumps(events(service.store))
    assert "secret-marker" not in json.dumps(events(service.store))


def test_cli_remote_adopt_refuses_a_duplicate_provider_id(service, capsys):
    remote_id = adopted_id(capsys, CLAUDE_URL)
    before = events(service.store)
    refusal = capture(capsys, "remote", "adopt", f"{CLAUDE_URL}?email={PRIVATE}", code=Exit.REFUSED)
    assert refusal.out == ""
    assert "already adopted" in refusal.err
    assert PRIVATE not in refusal.err
    assert events(service.store) == before
    rows = json.loads(capture(capsys, "remote", "list", "--json").out)["remote_jobs"]
    assert [row["remote_id"] for row in rows] == [remote_id]


@pytest.mark.parametrize(("url", "options", "diagnosis"), [
    (CLAUDE_URL, ["--account", PRIVATE], "label without an email address"),
    (f"https://claude.ai.example.invalid/code/session_Fake9?email={PRIVATE}", [], "session or Codex cloud task URL"),
    (f"https://chatgpt.com/codex/tasks/task_{SYNTHETIC_TOKEN}", [], "session or Codex cloud task URL"),
    (CLAUDE_URL, ["--name", f"metadata_{SYNTHETIC_TOKEN}"], "free of email addresses and credentials"),
], ids=["account-email", "url-host", "provider-id-credential", "metadata-credential"])
def test_cli_remote_adopt_refuses_invalid_input_without_retaining_it(service, capsys, url, options, diagnosis):
    before = events(service.store)
    refusal = capture(capsys, "remote", "adopt", url, *options, code=Exit.INVALID_INPUT)
    assert refusal.out == ""
    assert diagnosis in refusal.err
    assert PRIVATE not in refusal.err
    assert SYNTHETIC_TOKEN not in refusal.err
    assert events(service.store) == before
    assert "@" not in json.dumps(before)
    assert json.loads(capture(capsys, "remote", "list", "--json").out) == {"remote_jobs": []}


def test_cli_remote_forget_refuses_an_unknown_id(service, capsys):
    before = events(service.store)
    refusal = capture(capsys, "remote", "forget", "20261128-020000-missing", code=Exit.REFUSED)
    assert refusal.out == ""
    assert "unknown remote job" in refusal.err
    assert events(service.store) == before


def test_cli_online_status_includes_remote_rows_only_when_present(service, capsys):
    empty = capture(capsys, "status")
    assert "Remote" not in empty.out
    assert "remote_jobs" not in json.loads(capture(capsys, "status", "--json").out)

    remote_id = adopted_id(capsys, CODEX_URL)
    status = capture(capsys, "status").out
    assert all(word in status for word in ("Remote", "unpolled", remote_id, "running", CODEX_URL))
    assert "running jobs: 0\n" in status
    rows = json.loads(capture(capsys, "remote", "list", "--json").out)["remote_jobs"]
    assert len(rows) == 1 and rows[0]["last_polled_at"] is None
    assert json.loads(capture(capsys, "status", "--json").out)["remote_jobs"] == rows
    capture(capsys, "remote", "forget", remote_id)
    assert capture(capsys, "status") == empty
    assert "remote_jobs" not in json.loads(capture(capsys, "status", "--json").out)


def test_cli_offline_status_preserves_base_output_and_reads_remote_rows(service, tmp_path, capsys, monkeypatch):
    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 28, 2, tzinfo=timezone.utc).astimezone(tz)

    def unavailable(*args):
        raise DaemonUnavailable("test daemon unavailable")

    monkeypatch.setattr(offline, "datetime", FrozenClock)
    legacy = tmp_path / "legacy"
    with Store(legacy / "state.sqlite3") as store:
        with store.transaction() as tx:
            tx.execute("DROP TABLE IF EXISTS remote_jobs")
            tx.execute("UPDATE schema_version SET version=?", (NOTICE_WAKE_MIGRATION,))
    policy = json.loads((service.root / "policy.json").read_text())
    policy["claude_cards"]["enabled"] = False
    (legacy / "policy.json").write_text(json.dumps(policy))
    with monkeypatch.context() as down:
        down.setenv("SUBFLEET_HOME", str(legacy))
        down.setattr(cli, "_client", unavailable)
        # These are the base's two stdout forms for the same version-11 store and clock.
        base_text = (
            "last 24h: 0 runs with no shell or no writes; ids: -\n"
            "no lanes enrolled — subfleet lanes enroll <credential>\n\n"
            "claude reset cards: not read (claude_cards.enabled is false in the policy)\n\n"
            "running jobs: 0\n"
        )
        seen = capture(capsys, "status")
        assert seen.out == base_text
        assert seen.err == "subfleet status: offline — read from the store; readings are as of the last daemon write\n"
        base_json = {
            "offline": True, "now": NOW, "state_root": str(legacy), "schema_version": NOTICE_WAKE_MIGRATION,
            "restricted_runs_24h": [], "lanes": [], "readings": [], "weekly_samples": [], "closures": [],
            "running": [], "turns": [], "alerts": [], "task_holds": [],
            "claude_cards": {"read_at": None, "accounts": [], "warnings": [], "disabled": True},
        }
        assert capture(capsys, "status", "--json").out == json.dumps(base_json, sort_keys=True) + "\n"

    remote_id = adopted_id(capsys, CLAUDE_URL)
    rows = json.loads(capture(capsys, "remote", "list", "--json").out)["remote_jobs"]
    with monkeypatch.context() as down:
        down.setattr(cli, "_client", unavailable)
        assert json.loads(capture(capsys, "remote", "list", "--json").out) == {"remote_jobs": rows}
        status = capture(capsys, "status").out
        assert all(word in status for word in ("Remote", "unpolled", remote_id, "running", CLAUDE_URL))
        assert json.loads(capture(capsys, "status", "--json").out)["remote_jobs"] == rows
