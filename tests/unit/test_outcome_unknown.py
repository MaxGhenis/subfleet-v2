"""A lost answer is an unknown outcome, never "not done" (C-16.3, C-17.3, C-17.7).

Incident, 2026-09-24: a `subfleet run --batch` entry printed "no response from
the daemon within 15s" and "NOT submitted", yet the daemon had committed it
about a second before the client's deadline (event 581051). A manual retry
minted a fresh request id and created a duplicate job (event 581065). A kill
reported failed three times had committed its cancel (event 581283).

These tests drive the client and the CLI against the fake daemon of
`conftest.py`, whose handlers commit and then lose the answer (an empty reply
closes the connection; a sleep outlasts a shortened client deadline). Every
test names the clause it proves (C-20.5).
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from subfleet import cli, protocol
from subfleet import client as client_module
from subfleet.client import (Client, DaemonError, DaemonUnavailable, OutcomeUnknown,
                             ResponseLost)
from subfleet.contracts import Exit
from subfleet.protocol import ProtocolError

JOB = "20260924-074724-fv2-s4-thesis"
DEADLINE_S = 1.0                 # both deadlines, shortened; ample for a prompt answer
LATE_S = 1.6                     # an answer that outlasts them


@pytest.fixture(autouse=True)
def short_deadlines(monkeypatch):
    """The 15 s and 60 s deadlines, shortened so a late answer is lost quickly."""
    monkeypatch.setattr(client_module, "DEFAULT_TIMEOUT_S", DEADLINE_S)
    monkeypatch.setattr(client_module, "REQUERY_TIMEOUT_S", DEADLINE_S)


class Ledger:
    """The daemon's side of C-6.2: one job per request id, answers lost on demand.

    `lose` is consumed one submission at a time: "close" commits and then closes
    the connection without a line, "late" commits and answers after the client's
    deadline (the incident's shape), and None answers at once.
    """

    def __init__(self, *lose: str | None):
        self.lose = list(lose)
        self.jobs: dict[str, str] = {}           # request id -> job id

    def submit(self, request: protocol.Request):
        request_id = request.args["request_id"]
        created = request_id not in self.jobs
        if created:
            name = request.args.get("name") or "job"
            self.jobs[request_id] = f"20260924-0746{len(self.jobs):02d}-{name}"
        answer = {"job_id": self.jobs[request_id], "request_id": request_id,
                  "created": created, "state": "queued"}
        how = self.lose.pop(0) if self.lose else None
        if how == "close":
            return b""
        if how == "late":
            time.sleep(LATE_S)
        return answer

    def list(self, request: protocol.Request):
        # An older daemon ignores `request_id` (C-16.2): answer every job.
        return {"jobs": [{"job_id": job_id, "request_id": request_id, "state": "queued"}
                         for request_id, job_id in self.jobs.items()]}


def submits(server) -> list[dict]:
    return [request.args for request in server.requests if request.op == "submit"]


# --- the client: where "not sent" ends (C-16.3) ---------------------------------

def test_c16_3_a_timeout_after_connect_is_a_lost_response(daemon, root):
    """C-16.3 connecting proves the daemon may have the request; a timeout is not "not done"."""
    daemon({"submit": lambda request: (time.sleep(LATE_S), {"job_id": JOB})[1]})
    with pytest.raises(ResponseLost) as lost:
        Client(root).call("submit", {"request_id": "r-1"}, request_id="r-1")
    assert isinstance(lost.value, ProtocolError) and lost.value.code == Exit.OPERATIONAL
    assert str(lost.value) == "no response from the daemon within 1s"
    assert (lost.value.op, lost.value.request_id) == ("submit", "r-1")


def test_c16_3_a_close_without_a_line_is_a_lost_response(daemon, root):
    """C-16.3 the daemon closed after reading the request: its outcome is unknown."""
    daemon({"kill": lambda request: b""})
    with pytest.raises(ResponseLost, match="closed the connection without a response"):
        Client(root).call("kill", {"job_id": JOB})


def test_c16_3_a_reset_after_connect_is_a_lost_response(daemon, root, monkeypatch):
    """C-16.3 a reset while reading (or a failed send) may follow a delivered request."""
    daemon({"kill": lambda request: {"status": "cancel requested"}})

    def reset(conn, deadline_at):
        raise ConnectionResetError(54, "Connection reset by peer")
    monkeypatch.setattr(client_module, "_read_line", reset)
    with pytest.raises(ResponseLost, match="daemon connection failed"):
        Client(root).call("kill", {"job_id": JOB})


def test_c16_3_a_peer_that_hangs_up_at_once_is_a_lost_response(root):
    """C-16.3 whether the hang-up shows as a broken pipe or as EOF, nothing says "not done"."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(root / "daemon.sock"))
    server.listen(1)
    try:
        def hang_up():
            conn, _ = server.accept()
            conn.close()
        threading.Thread(target=hang_up, daemon=True).start()
        with pytest.raises(ResponseLost):
            Client(root).call("submit", {"request_id": "r-2"}, request_id="r-2")
    finally:
        server.close()


@pytest.mark.parametrize("body,expected", [
    (b"{not json\n", "malformed response"),
    (b'{"v": 1, "ok": false, "error": 7}\n', "malformed response"),
    (b"x" * 4096, "without a newline"),
])
def test_c16_3_an_undecodable_answer_is_a_lost_response(daemon, root, monkeypatch, body, expected):
    """C-16.3 an oversize or malformed line settles nothing about the request."""
    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 1024)
    daemon({"submit": lambda request: body})
    with pytest.raises(ResponseLost, match=expected):
        Client(root).call("submit", {"request_id": "r-3"}, request_id="r-3")


def test_c16_3_a_refused_connection_is_not_sent(root):
    """C-16.3, C-17.3 before connect nothing was sent: that stays DaemonUnavailable (69)."""
    with pytest.raises(DaemonUnavailable):
        Client(root).call("submit", {})                       # no socket file at all
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(root / "daemon.sock"))                     # a file nobody listens on
    stale.close()
    with pytest.raises(DaemonUnavailable) as refused:
        Client(root).call("submit", {})
    assert not isinstance(refused.value, ResponseLost)


def test_c16_3_another_protocol_version_is_not_a_lost_response(daemon, root):
    """C-16.1 a well-formed answer in another version is an answer, not a lost one."""
    daemon({"submit": lambda request: b'{"v": 2, "ok": true, "result": {}}\n'})
    with pytest.raises(ProtocolError, match="protocol version 2") as error:
        Client(root).call("submit", {})
    assert not isinstance(error.value, ResponseLost)


# --- the settle helper (C-16.3) --------------------------------------------------

class Scripted(Client):
    """A client whose `call` plays a script: each step is a result or an exception."""

    def __init__(self, root, *script):
        super().__init__(root)
        self.script = list(script)
        self.sent: list[tuple] = []

    def call(self, op, args=None, *, request_id="", timeout=None):
        self.sent.append((op, args, request_id, timeout))
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def lost(message="no response from the daemon within 15s"):
    return ResponseLost(message, op="submit", request_id="rid")


def test_c16_3_a_lost_answer_is_asked_again_with_the_same_request(root, monkeypatch):
    """C-16.3, C-6.2 the identical request, same id, once more, with the 60 s deadline."""
    monkeypatch.setattr(client_module, "REQUERY_TIMEOUT_S", 60.0)
    told = []
    client = Scripted(root, lost(), {"job_id": JOB, "created": False})
    result = client.call_settled("submit", {"request_id": "rid"}, request_id="rid",
                                 on_lost=told.append)
    assert result == {"job_id": JOB, "created": False, "requeried": True}
    assert [step[:3] for step in client.sent] == [("submit", {"request_id": "rid"}, "rid")] * 2
    assert client.sent[0][3] is None and client.sent[1][3] == 60.0
    assert len(told) == 1 and "within 15s" in str(told[0])


def test_c16_3_an_answered_request_is_never_sent_twice(root):
    """C-16.3 only a lost answer is re-sent; a refusal and a refused connect are final."""
    assert Scripted(root, {"job_id": JOB}).call_settled("submit", {}, request_id="r") == {"job_id": JOB}
    refused = Scripted(root, DaemonError(7, "refused"))
    with pytest.raises(DaemonError):
        refused.call_settled("submit", {}, request_id="r")
    down = Scripted(root, DaemonUnavailable("no daemon"))
    with pytest.raises(DaemonUnavailable):
        down.call_settled("submit", {}, request_id="r")
    assert len(refused.sent) == len(down.sent) == 1


def test_c16_3_lost_twice_is_an_unknown_outcome(root):
    """C-16.3, C-17.3 answered neither time: OutcomeUnknown, exit 1, both reasons, the id."""
    client = Scripted(root, lost(), lost("the daemon closed the connection without a response"))
    with pytest.raises(OutcomeUnknown) as unknown:
        client.call_settled("submit", {}, request_id="rid")
    error = unknown.value
    assert isinstance(error, ResponseLost) and isinstance(error, ProtocolError)
    assert error.code == Exit.OPERATIONAL and error.request_id == "rid" and error.op == "submit"
    assert error.reasons == ("no response from the daemon within 15s",
                             "the daemon closed the connection without a response")


def test_c16_3_a_daemon_gone_before_the_re_send(root):
    """C-16.3, C-17.5 a submit may have committed (unknown); a kill falls back offline."""
    submit = Scripted(root, lost(), DaemonUnavailable("no daemon at daemon.sock"))
    with pytest.raises(OutcomeUnknown, match="went away"):
        submit.call_settled("submit", {}, request_id="rid")
    kill = Scripted(root, lost(), DaemonUnavailable("no daemon at daemon.sock"))
    with pytest.raises(DaemonUnavailable):
        kill.call_settled("kill", {"job_id": JOB})


def test_c16_3_a_refused_re_send_looks_the_request_id_up(root):
    """C-16.3, C-6.2 a minted id's job is this invocation's, even when the re-send is refused."""
    refusal = DaemonError(2, "request id already used with a different payload")
    rows = {"jobs": [{"job_id": "someone-else", "request_id": "other"},
                     {"job_id": JOB, "request_id": "rid", "state": "queued"}]}
    client = Scripted(root, lost(), refusal, rows)
    result = client.call_settled("submit", {}, request_id="rid", minted=True)
    assert result["job_id"] == JOB and result["requeried"] is True
    assert "different payload" in result["refused"]
    assert client.sent[2][:2] == ("list", {"mine": None, "running": False, "last": None,
                                           "request_id": "rid"})


def test_c16_3_a_supplied_id_keeps_the_refusal_and_names_the_job(root):
    """C-16.3 a caller-supplied id may belong to an earlier run: exit 2, naming the job."""
    refusal = DaemonError(2, "request id already used with a different payload", None)
    client = Scripted(root, lost(), refusal, {"jobs": [{"job_id": JOB, "request_id": "rid"}]})
    with pytest.raises(DaemonError) as refused:
        client.call_settled("submit", {}, request_id="rid", minted=False)
    assert refused.value.code == 2 and JOB in str(refused.value)
    assert refused.value.fix == f"subfleet runs show {JOB}"


def test_c16_3_a_refusal_stands_when_no_job_carries_the_id(root):
    """C-16.3 found nowhere, the refusal of the re-send is the answer."""
    refusal = DaemonError(7, "writable job refused on main")
    client = Scripted(root, lost(), refusal, {"jobs": [{"job_id": "x", "request_id": "other"}]})
    with pytest.raises(DaemonError) as refused:
        client.call_settled("submit", {}, request_id="rid", minted=True)
    assert refused.value is refusal


def test_c16_3_a_failed_lookup_leaves_the_outcome_unknown(root):
    """C-16.3 refused, and the id could not be looked up: nothing settles it."""
    client = Scripted(root, lost(), DaemonError(2, "different payload"),
                      ResponseLost("no response from the daemon within 15s"))
    with pytest.raises(OutcomeUnknown) as unknown:
        client.call_settled("submit", {}, request_id="rid", minted=True)
    assert len(unknown.value.reasons) == 3 and "looked up" in unknown.value.reasons[2]


def test_c16_3_a_kill_refused_on_re_send_is_refused(root):
    """C-7.1 a kill answer after the re-send is authoritative; nothing is looked up."""
    client = Scripted(root, lost(), DaemonError(2, "no such job"))
    with pytest.raises(DaemonError, match="no such job"):
        client.call_settled("kill", {"job_id": JOB})
    assert [step[0] for step in client.sent] == ["kill", "kill"]


# --- run (C-16.3, C-17.3) --------------------------------------------------------

@pytest.mark.parametrize("how", ["close", "late"])
def test_c16_3_run_lost_once_prints_the_one_job(daemon, capsys, workdir, how):
    """C-16.3, C-6.2 committed, answer lost, asked again: one job, exit 0, its id on stdout."""
    ledger = Ledger(how)
    server = daemon({"submit": ledger.submit})
    assert cli.main(["run", "-m", "opus", "-C", str(workdir), "-d", "-n", "fv2-s2", "hi"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == next(iter(ledger.jobs.values()))
    assert len(ledger.jobs) == 1
    sent = submits(server)
    assert len(sent) == 2 and sent[0]["request_id"] == sent[1]["request_id"]
    assert sent[0] == sent[1], "the re-send is the identical request"
    assert "sending the same submission again" in captured.err
    assert "acknowledged on re-query: the first, unanswered submission created it" in captured.err


def test_c16_3_run_json_reports_the_job_as_this_invocations(daemon, capsys, workdir):
    """C-17.4 with a minted id the job is this run's own, though the re-send said created: false."""
    ledger = Ledger("close")
    daemon({"submit": ledger.submit})
    assert cli.main(["run", "-m", "opus", "-C", str(workdir), "-d", "--json", "hi"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] is True and payload["requeried"] is True
    assert payload["outcome"] == "created" and payload["job_id"] in ledger.jobs.values()


def test_c16_3_run_lost_twice_is_unknown_and_names_the_request_id(daemon, capsys, workdir):
    """C-16.3, C-17.3 exit 1, no job id on stdout, the request id and the settling command."""
    ledger = Ledger("close", "late")
    server = daemon({"submit": ledger.submit})
    assert cli.main(["run", "-m", "opus", "-C", str(workdir), "-d", "hi"]) == Exit.OPERATIONAL
    captured = capsys.readouterr()
    request_id = submits(server)[0]["request_id"]
    assert captured.out == ""
    assert "outcome unknown" in captured.err and request_id in captured.err
    assert f"--request-id {request_id}" in captured.err
    assert "NOT submitted" not in captured.err and "no response from the daemon" in captured.err
    # The daemon did create it, which is why the CLI must not say otherwise.
    assert len(ledger.jobs) == 1
    # Settling: the same command with that request id finds the one job.
    assert cli.main(["run", "-m", "opus", "-C", str(workdir), "-d",
                     "--request-id", request_id, "hi"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == ledger.jobs[request_id]
    assert "existing job for this request id" in captured.err
    assert len(ledger.jobs) == 1


def test_c16_3_run_lost_twice_json(daemon, capsys, workdir):
    """C-17.4 one object: job_id null, outcome unknown, the request id, the error."""
    ledger = Ledger("close", "close")
    server = daemon({"submit": ledger.submit})
    assert cli.main(["run", "-m", "opus", "-C", str(workdir), "-d", "--json", "hi"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"job_id": None, "request_id": submits(server)[0]["request_id"],
                       "outcome": "unknown", "error": payload["error"]}
    assert "outcome of submit is unknown" in payload["error"]


def refuse_second(ledger: Ledger, message: str = "request id already used with a different payload"):
    """A daemon whose checkout moved between the two executions (C-6.2)."""
    def submit(request):
        if any(request.args["request_id"] == known for known in ledger.jobs):
            return protocol.fail(request.id, Exit.INVALID_INPUT, message)
        ledger.submit(request)
        return b""                                  # committed; answer lost
    return submit


def test_c16_3_run_minted_id_refused_on_re_send_finds_its_job(daemon, capsys, workdir):
    """C-16.3 a different-payload answer proves the job exists; a minted id makes it this run's."""
    ledger = Ledger()
    ledger.jobs["unrelated"] = "20260924-070000-unrelated"
    server = daemon({"submit": refuse_second(ledger), "list": ledger.list})
    assert cli.main(["run", "-m", "opus", "-C", str(workdir), "-d", "-n", "fv2-s2", "hi"]) == 0
    captured = capsys.readouterr()
    request_id = submits(server)[0]["request_id"]
    assert captured.out.strip() == ledger.jobs[request_id] != "20260924-070000-unrelated"
    assert server.args("list")["request_id"] == request_id
    assert "found by its request id" in captured.err


def test_c16_3_run_supplied_id_refused_on_re_send_names_the_job(daemon, capsys, workdir):
    """C-16.3 with --request-id the refusal stands (exit 2) and names the job holding the id."""
    ledger = Ledger()
    daemon({"submit": refuse_second(ledger), "list": ledger.list})
    assert cli.main(["run", "-m", "opus", "-C", str(workdir), "-d",
                     "--request-id", "fv2-s2", "hi"]) == Exit.INVALID_INPUT
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "different payload" in captured.err and ledger.jobs["fv2-s2"] in captured.err


def test_c16_3_a_dry_run_is_not_re_sent(daemon, capsys, workdir):
    """C-16.3 a dry run creates nothing, so a lost answer is a plain error, not unknown."""
    server = daemon({"submit": lambda request: b""})
    assert cli.main(["run", "-m", "opus", "-C", str(workdir), "--dry-run", "hi"]) == 1
    assert len(submits(server)) == 1
    assert "outcome unknown" not in capsys.readouterr().err


# --- resume (C-16.3) ---------------------------------------------------------------

def show_source(root: Path):
    return lambda request: {"job_id": JOB, "workdir": str(root), "sandbox": "read-only",
                            "lane_id": "codex-3", "name": "demo", "state": "succeeded"}


def test_c16_3_resume_lost_once_then_answered(daemon, capsys, root):
    """C-16.3 a resume is a submit: its lost answer is asked again under the same id."""
    ledger = Ledger("close")
    server = daemon({"show": show_source(root), "submit": ledger.submit})
    assert cli.main(["resume", JOB, "keep going"]) == 0
    assert capsys.readouterr().out.strip() in ledger.jobs.values()
    sent = submits(server)
    assert len(sent) == 2 and sent[0] == sent[1] and len(ledger.jobs) == 1


def test_c16_3_resume_lost_twice_is_unknown(daemon, capsys, root):
    """C-16.3, C-17.3 exit 1 and the request id; --json says so in one object."""
    ledger = Ledger("close", "close")
    server = daemon({"show": show_source(root), "submit": ledger.submit})
    assert cli.main(["resume", JOB, "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "unknown" and payload["job_id"] is None
    assert payload["resumed_from"] == JOB
    assert payload["request_id"] == submits(server)[0]["request_id"]


# --- run --batch (C-17.7) ------------------------------------------------------------

@pytest.fixture
def manifest(tmp_path, workdir) -> Path:
    path = tmp_path / "fv2.json"
    path.write_text(json.dumps({"label": "fv2", "defaults": {"model": "opus", "workdir": str(workdir)},
                                "jobs": [{"name": f"fv2-s{n}", "prompt_text": f"brief {n}"}
                                         for n in (1, 2, 3)]}))
    return path


def by_entry(ledger: Ledger, lose: dict[int, list[str | None]]):
    """Lose the answers of one entry, by manifest index, the given number of times."""
    def submit(request):
        plan = lose.get(request.args["batch"]["index"], [])
        how = plan.pop(0) if plan else None
        ledger.lose = [how]
        return ledger.submit(request)
    return submit


def test_c17_7_entry_ids_derive_from_the_batch_id(daemon, capsys, manifest):
    """C-17.7 without --request-id, entry n is <fresh batch id>-<n>, so the batch can be re-run."""
    ledger = Ledger()
    server = daemon({"submit": ledger.submit})
    assert cli.main(["run", "--batch", str(manifest), "-d", "--json"]) == 0
    sent = submits(server)
    batch_id = sent[0]["batch"]["id"]
    assert [args["request_id"] for args in sent] == [f"{batch_id}-{n}" for n in (1, 2, 3)]
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["outcome"] for row in rows] == ["created"] * 3


def test_c17_7_a_request_id_too_long_for_its_entries_is_refused(daemon, capsys, manifest):
    """C-1.5, C-17.7 <id>-<n> must fit the 128-character request id; nothing is sent."""
    server = daemon({"submit": Ledger().submit})
    assert cli.main(["run", "--batch", str(manifest), "-d", "--request-id", "x" * 127]) == 2
    assert "exceed 128" in capsys.readouterr().err and submits(server) == []


def test_c17_7_an_entry_lost_once_is_asked_again(daemon, capsys, manifest):
    """C-16.3, C-17.7 the lost entry is re-sent under its own id and the batch goes on."""
    ledger = Ledger()
    server = daemon({"submit": by_entry(ledger, {2: ["late"]})})
    assert cli.main(["run", "--batch", str(manifest), "-d"]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == list(ledger.jobs.values())
    assert [args["batch"]["index"] for args in submits(server)] == [1, 2, 2, 3]
    assert "3 of 3 submitted" in captured.err
    assert "acknowledged on re-query" in captured.err


def test_c17_7_an_entry_lost_twice_stops_the_batch(daemon, capsys, manifest):
    """C-16.3, C-17.7 unknown, then not sent; never "NOT submitted"; the re-run hint."""
    ledger = Ledger()
    server = daemon({"submit": by_entry(ledger, {2: ["close", "late"]})})
    assert cli.main(["run", "--batch", str(manifest), "-d", "--json"]) == Exit.OPERATIONAL
    captured = capsys.readouterr()
    rows = [json.loads(line) for line in captured.out.splitlines()]
    assert [row["outcome"] for row in rows] == ["created", "unknown", "not-sent"]
    assert [row["rc"] for row in rows] == [0, 1, 1]
    assert rows[1]["job_id"] is None and rows[2]["job_id"] is None
    batch_id = rows[0]["batch"]
    assert rows[1]["request_id"] == f"{batch_id}-2" and "unknown" in rows[1]["error"]
    rerun = f"run --batch {manifest.resolve()} --request-id {batch_id}"
    assert rows[1]["fix"].endswith(rerun) and rows[2]["fix"].endswith(rerun)
    # Entry 3 was never sent: a daemon that did not answer twice is not answering.
    assert [args["batch"]["index"] for args in submits(server)] == [1, 2, 2]
    assert rerun in captured.err and "NOT submitted" not in captured.err
    # The daemon holds entry 2 even so.
    assert f"{batch_id}-2" in ledger.jobs


def test_c17_7_the_table_says_unknown_and_not_sent(daemon, capsys, manifest):
    """C-17.7 stderr distinguishes the three failures; stdout carries only real job ids."""
    ledger = Ledger()
    daemon({"submit": by_entry(ledger, {2: ["close", "close"]})})
    assert cli.main(["run", "--batch", str(manifest), "-d"]) == 1
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [ledger.jobs[next(iter(ledger.jobs))]]
    assert "[2] fv2-s2 outcome unknown" in captured.err
    assert "[3] fv2-s3 not sent" in captured.err
    assert "NOT submitted" not in captured.err
    # C-16.3: the unknown entry names the lookup that finds it by request id.
    assert "look: subfleet runs --request-id " in captured.err


def test_c17_7_re_running_with_the_batch_id_creates_nothing_twice(daemon, capsys, manifest):
    """C-6.2, C-17.7 the hint's command re-sends the same ids; each entry ends with one job."""
    ledger = Ledger()
    server = daemon({"submit": by_entry(ledger, {2: ["close", "close"]})})
    assert cli.main(["run", "--batch", str(manifest), "-d", "--json"]) == 1
    batch_id = json.loads(capsys.readouterr().out.splitlines()[0])["batch"]
    first = [args["request_id"] for args in submits(server)]
    assert cli.main(["run", "--batch", str(manifest), "-d", "--json",
                     "--request-id", batch_id]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    again = [args["request_id"] for args in submits(server)][len(first):]
    assert again == [f"{batch_id}-{n}" for n in (1, 2, 3)]
    assert set(first) <= set(again)
    assert [row["outcome"] for row in rows] == ["existing", "existing", "created"]
    assert len(ledger.jobs) == 3


def test_c17_7_first_failure_rule_survives_an_unknown_entry(daemon, capsys, manifest):
    """C-17.7 a refusal before the unknown entry keeps its code as the batch's."""
    ledger = Ledger()
    plan = {3: ["close", "close"]}

    def submit(request):
        if request.args["batch"]["index"] == 1:
            return protocol.fail(request.id, Exit.REFUSED, "writable job refused on main",
                                 "check out a task branch")
        return by_entry(ledger, plan)(request)
    daemon({"submit": submit})
    assert cli.main(["run", "--batch", str(manifest), "-d"]) == Exit.REFUSED
    err = capsys.readouterr().err
    assert "[1] fv2-s1 NOT submitted (rc 7)" in err and "[3] fv2-s3 outcome unknown" in err


def test_c17_7_a_daemon_gone_mid_batch_reports_every_entry(daemon, capsys, manifest, root):
    """C-17.7 exit 69; entries after the refused connect are not sent; the re-run hint."""
    ledger = Ledger()

    def submit(request):
        if request.args["batch"]["index"] == 1:
            os.unlink(root / "daemon.sock")          # the next connect is refused
        return ledger.submit(request)
    daemon({"submit": submit})
    assert cli.main(["run", "--batch", str(manifest), "-d", "--json"]) == Exit.DAEMON_UNAVAILABLE
    captured = capsys.readouterr()
    rows = [json.loads(line) for line in captured.out.splitlines()]
    assert [row["outcome"] for row in rows] == ["created", "not-sent", "not-sent"]
    assert rows[1]["rc"] == rows[2]["rc"] == 69
    assert f"--request-id {rows[0]['batch']}" in captured.err
    assert "went away after 1 of 3" in captured.err and "subfleet daemon start" in captured.err


# --- kill (C-7.1, C-16.3) --------------------------------------------------------------

def kill_lost(times: int, answer: dict):
    """Lose the first `times` answers of each `kill` invocation's requests."""
    def kill(request):
        kill.calls.append(request.args)
        return b"" if len(kill.calls) <= times else answer
    kill.calls = []
    return kill


def test_c16_3_kill_lost_once_is_asked_again(daemon, capsys):
    """C-7.1, C-16.3 a cancel is recorded once however often it is sent; the re-send answers."""
    server = daemon({"kill": kill_lost(1, {"status": "cancel requested"})})
    assert cli.main(["kill", JOB]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == f"{JOB} cancel requested"
    assert [op for op in server.ops()] == ["kill", "kill"]
    assert "acknowledged on re-query" in captured.err


def test_c16_3_kill_already_finished_on_re_query_shows_what_it_became(daemon, capsys):
    """C-7.2, C-16.3 the first, unanswered kill may be what finished it: state and rc."""
    kill = kill_lost(1, {"status": "already finished"})
    server = daemon({"kill": kill,
                     "show": lambda request: {"job": {"job_id": JOB, "state": "cancelled", "rc": 130,
                                                      "cancel_requested_at": "2026-09-24T11:59:01Z"}}})
    assert cli.main(["kill", JOB]) == 0
    assert capsys.readouterr().out.strip() == f"{JOB} already finished (cancelled, rc 130)"
    assert server.ops() == ["kill", "kill", "show"]
    kill.calls.clear()
    assert cli.main(["kill", JOB, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "cancelled" and payload["rc"] == 130 and payload["requeried"] is True


def test_c16_3_kill_lost_twice_is_unknown_not_failed(daemon, capsys):
    """C-16.3, C-17.3 exit 1, "outcome unknown", safe to repeat; not offline, not waited on."""
    kill = kill_lost(2, {"status": "cancel requested"})
    server = daemon({"kill": kill,
                     "wait": lambda request: {"jobs": {JOB: {"job_id": JOB, "state": "cancelled"}}}})
    assert cli.main(["kill", JOB, "--wait"]) == Exit.OPERATIONAL
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "outcome unknown" in captured.err and f"subfleet kill {JOB} again is safe" in captured.err
    assert f"subfleet runs show {JOB}" in captured.err and "cancel_requested_at" in captured.err
    assert "offline" not in captured.err and "failed" not in captured.err
    assert "refused" not in captured.err
    assert server.ops() == ["kill", "kill"], "an unsettled kill is not waited on"
    kill.calls.clear()
    assert cli.main(["kill", JOB, "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["job_id"] == JOB and payload["outcome"] == "unknown"


def test_c17_5_kill_whose_daemon_went_away_falls_back_offline(daemon, capsys, root):
    """C-16.3, C-17.5 the re-send finds no daemon: the offline kill path, as before."""
    def kill(request):
        os.unlink(root / "daemon.sock")
        return b""
    daemon({"kill": kill})
    assert cli.main(["kill", JOB]) == Exit.DAEMON_UNAVAILABLE
    err = capsys.readouterr().err
    assert "outcome unknown" not in err and "subfleet daemon start" in err


# --- the sessions kit (C-16.3, C-23.54) -----------------------------------------------

def test_c16_3_sessions_submit_settles_a_lost_answer(daemon, root, workdir):
    """C-23.54 the kit submits through the ordinary path, so it settles as `run` does."""
    from subfleet.sessions.client import Sessions
    ledger = Ledger("close")
    server = daemon({"submit": ledger.submit})
    told = []
    args = protocol.SubmitArgs(request_id="revive-1", kind="revive", workdir=str(workdir),
                               prompt_path=str(workdir / "p.md"), sandbox="read-only",
                               pinned_model="opus")
    result = Sessions(Client(root), on_lost=told.append).submit(args)
    assert result["job_id"] == ledger.jobs["revive-1"] and result["requeried"] is True
    assert len(submits(server)) == 2 and len(told) == 1
    ledger.lose = ["close", "close"]
    with pytest.raises(OutcomeUnknown):
        Sessions(Client(root)).submit(protocol.SubmitArgs(
            request_id="revive-2", kind="revive", workdir=str(workdir),
            prompt_path=str(workdir / "p.md"), sandbox="read-only", pinned_model="opus"))


def test_c16_3_sessions_verbs_report_an_unknown_outcome(monkeypatch, capsys, root):
    """C-16.3, C-17.3 a revive or a handoff sent twice unanswered is exit 1, never "refused"."""
    from subfleet.sessions import cli as sessions_cli
    from subfleet.sessions import handoff as handoff_module
    from subfleet.sessions import revive as revive_module
    unknown = OutcomeUnknown("submit", "rid-7", ("no response from the daemon within 15s",
                                                 "no response from the daemon within 60s"))

    def raise_unknown(*args, **kwargs):
        raise unknown
    monkeypatch.setattr(sessions_cli, "_policy", lambda args: {})
    monkeypatch.setattr(revive_module, "revive", raise_unknown)
    assert cli.main(["sessions", "revive", "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"]) == 1
    err = capsys.readouterr().err
    assert "outcome unknown" in err and "rid-7" in err and "look before running it again" in err
    monkeypatch.setattr(handoff_module, "handoff", raise_unknown)
    monkeypatch.setattr(sessions_cli.Sessions, "state", lambda self, ids=None: {})
    assert cli.main(["handoff", "--last", "--to", "opus", "--json"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"job_id": None, "request_id": "rid-7",
                                        "outcome": "unknown", "error": str(unknown)}
    # A handoff's brief is rebuilt from a growing transcript: the lookup settles it.
    assert "settle it: subfleet runs --request-id rid-7 --json" in captured.err
    assert "a re-run with --request-id rid-7 rebuilds the payload" in captured.err


# --- review round 1 (2026-09-25): provenance, lookups, resolutions, quoting ------

def _lost_then_moved(request_id: str):
    """A daemon that commits the first submit and loses its answer, then refuses the
    re-send as a different payload (HEAD moved), naming the job (C-6.2, C-16.3)."""
    sent = []

    def submit(request):
        sent.append(request)
        if len(sent) == 1:
            return b""                                   # committed, answer lost
        return protocol.fail(request.id, 2, "request id already used with a different "
                             "payload by job 20260925-120000-revive")

    def listing(request):
        wanted = request.args.get("request_id")
        return {"jobs": [{"job_id": "20260925-120000-revive", "request_id": request_id,
                          "state": "queued"}] if wanted == request_id else []}
    return {"submit": submit, "list": listing}, sent


@pytest.mark.parametrize("minted", [True, False])
def test_c16_3_the_sessions_kit_keeps_whose_request_id_it_is(daemon, root, workdir, minted):
    """C-16.3: a revive or handoff that minted its id finds its own job after a refused
    re-send; an operator-supplied id keeps the refusal, naming the job."""
    from subfleet.sessions.client import Sessions
    handlers, sent = _lost_then_moved("revive-9")
    daemon(handlers)
    args = protocol.SubmitArgs(request_id="revive-9", kind="revive", workdir=str(workdir),
                               prompt_path=str(workdir / "p.md"), sandbox="read-only",
                               pinned_model="opus")
    if minted:
        result = Sessions(Client(root)).submit(args, minted=True)
        assert result["job_id"] == "20260925-120000-revive" and result["requeried"] is True
    else:
        with pytest.raises(DaemonError, match="20260925-120000-revive"):
            Sessions(Client(root)).submit(args)
    assert len(sent) == 2


def test_c16_3_runs_finds_a_job_by_request_id_whoever_submitted_it(daemon, capsys, monkeypatch):
    """C-16.3, C-17.1: `runs --request-id` sends the filter, ignores --last, and filters the
    rows itself for a daemon that ignores the field (C-16.2)."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "session-a")
    rows = [{"job_id": "j-1", "request_id": "other", "state": "succeeded"},
            {"job_id": "j-2", "request_id": "rid-x", "state": "queued"}]
    server = daemon({"list": lambda request: {"jobs": rows}})      # an older daemon
    assert cli.main(["runs", "--request-id", "rid-x", "--json"]) == 0
    assert server.args("list")["request_id"] == "rid-x" and server.args("list")["last"] is None
    assert server.args("list")["mine"] is None
    assert [json.loads(line)["job_id"] for line in capsys.readouterr().out.splitlines()] == ["j-2"]


def test_c16_3_the_unknown_outcome_lookup_is_by_request_id_and_quoted():
    """C-16.3: the lookup hint finds the job whoever its caller is, and a request id with
    shell metacharacters is quoted, so pasting it cannot change the id."""
    assert cli._look_command("rid-1") == "subfleet runs --request-id rid-1 --json"
    assert cli._look_command("batch$USER") == "subfleet runs --request-id 'batch$USER' --json"
    assert "--mine" not in cli._look_command("rid-1")


def test_c17_7_the_rerun_command_quotes_the_batch_id(daemon, capsys, manifest):
    """C-17.7, C-16.3: a batch id with `$` in it survives being pasted into a shell."""
    daemon({"submit": lambda request: b""})
    assert cli.main(["run", "--batch", str(manifest), "--request-id", "batch$USER"]) == 1
    err = capsys.readouterr().err
    assert "--request-id 'batch$USER'" in err and "--request-id batch$USER" not in err


@pytest.mark.parametrize("flag", ["--confirm-dead", "--force-release"])
def test_c16_3_an_unknown_resolution_says_to_repeat_the_resolution(daemon, capsys, flag):
    """C-16.3, C-5.7: a quarantine resolution sent twice unanswered is retried with its own
    flag and note; plain `kill` would only answer "already finished"."""
    daemon({"kill": lambda request: b""})
    assert cli.main(["kill", "JOB$1", flag, "--note", "checked by hand"]) == 1
    err = capsys.readouterr().err
    assert f"the {flag} resolution may have been requested" in err
    assert f"subfleet kill 'JOB$1' {flag} --note 'checked by hand' again is safe" in err
    assert "subfleet runs show 'JOB$1' shows whether the attempt is still quarantined" in err
    assert "cancel_requested_at" not in err and "leases" not in err


class _Captured(Exception):
    """Stops a sessions verb at the kit call, carrying the arguments it was given."""


@pytest.mark.parametrize("argv,minted", [
    (["sessions", "revive", "3f9c1a2e-7b40-4d51-9a8e-2c6f0b1d4e77"], True),
    (["handoff", "--last", "--to", "opus"], True),
    (["handoff", "--last", "--to", "opus", "--request-id", "operator-rid"], False),
])
def test_c16_3_the_sessions_verbs_pass_whose_request_id_it_is(monkeypatch, root, argv, minted):
    """C-16.3 (review round 2): the CLI mints the id before calling revive/handoff (the
    staged prompt is named after it), so it must say it minted it; only an operator's
    --request-id is not minted. Revive and handoff pass it on to the kit (tested in
    test_sessions_revive/handoff), and the kit settles with it (tested above)."""
    from subfleet.sessions import cli as sessions_cli
    from subfleet.sessions import handoff as handoff_module
    from subfleet.sessions import revive as revive_module

    def capture(*args, **kwargs):
        raise _Captured(kwargs)
    monkeypatch.setattr(sessions_cli, "_policy", lambda args: {})
    monkeypatch.setattr(sessions_cli.Sessions, "state", lambda self, ids=None: {})
    monkeypatch.setattr(revive_module, "revive", capture)
    monkeypatch.setattr(handoff_module, "handoff", capture)
    with pytest.raises(_Captured) as caught:
        cli.main(argv)
    kwargs = caught.value.args[0]
    assert kwargs["minted"] is minted
    assert kwargs["request_id"] == ("operator-rid" if not minted else kwargs["request_id"])


def test_c16_3_every_sessions_call_into_revive_or_handoff_says_whose_id_it_is():
    """C-16.3: `sessions continue` mints each revive's or handoff's id itself, as
    `sessions revive` does; every call from the sessions CLI names `minted`, so a
    new call site cannot silently fall back to "the operator's id"."""
    import ast
    import inspect
    from subfleet.sessions import cli as sessions_cli
    calls = [node for node in ast.walk(ast.parse(inspect.getsource(sessions_cli)))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and isinstance(node.func.value, ast.Name)
             and (node.func.value.id, node.func.attr) in {("revive_module", "revive"),
                                                          ("handoff_module", "handoff")}]
    assert len(calls) == 4                     # continue (revive, handoff), revive, handoff
    assert all(any(k.arg == "minted" for k in call.keywords) for call in calls)
    constant = [next(k.value.value for k in call.keywords if k.arg == "minted")
                for call in calls
                if isinstance(next(k.value for k in call.keywords if k.arg == "minted"), ast.Constant)]
    assert constant == [True, True, True]      # the paths that mint the id just above
