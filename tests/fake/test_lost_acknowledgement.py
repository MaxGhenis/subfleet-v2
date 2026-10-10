"""A real daemon whose answer the client loses (C-16.3, C-6.2, C-7.1, C-7.2).

Incident, 2026-09-24: the daemon committed a submit (event 581051) and a cancel
(event 581283) whose answers the client never read, and the CLI reported both
as failures; the submit's manual retry, under a fresh request id, created a
duplicate job. Here the client reads the daemon's real answer and then drops
it, as a client whose deadline expired a moment too early would, so every
assertion is about what the daemon actually committed.
"""

from __future__ import annotations

import json

from subfleet import cli
from subfleet import client as client_module
from subfleet.client import Client


def drop_first_answer(monkeypatch) -> list[bytes]:
    """Read the first complete answer off the socket, then behave as if it never came."""
    real = client_module._read_line
    dropped: list[bytes] = []

    def lossy(conn, deadline_at):
        line = real(conn, deadline_at)
        if not dropped:
            dropped.append(line)
            raise TimeoutError("the answer arrived after the client stopped reading")
        return line
    monkeypatch.setattr(client_module, "_read_line", lossy)
    return dropped


def test_c16_3_a_committed_submit_whose_answer_is_lost_settles_to_its_one_job(daemon, monkeypatch):
    """C-16.3, C-6.2 the re-send under the same request id answers with the job the first created."""
    daemon.start()
    args = daemon.submit_args()
    dropped = drop_first_answer(monkeypatch)
    told = []
    result = Client(daemon.root).call_settled("submit", args, request_id=args["request_id"],
                                              minted=True, on_lost=told.append)
    first = json.loads(dropped[0])
    assert first["ok"] and first["result"]["created"] is True, "the first submission committed"
    assert result["job_id"] == first["result"]["job_id"]
    assert result["created"] is False and result["requeried"] is True
    assert len(told) == 1
    assert daemon.rows("SELECT job_id FROM jobs WHERE request_id=?",
                       (args["request_id"],)) == [{"job_id": result["job_id"]}]
    assert len(daemon.rows("SELECT event_id FROM events WHERE kind='job.submitted' AND job_id=?",
                           (result["job_id"],))) == 1


def test_c16_3_list_finds_a_job_by_its_request_id(daemon):
    """C-16.3 `list` filters on `request_id`, and a moved payload's refusal names the job."""
    daemon.start()
    ours = daemon.submit_args()
    job_id = daemon.call("submit", **ours)["job_id"]
    daemon.submit()                                         # another job, another id
    listed = daemon.call("list", request_id=ours["request_id"])["jobs"]
    assert [row["job_id"] for row in listed] == [job_id]
    assert Client(daemon.root).find_request(ours["request_id"])["job_id"] == job_id
    assert Client(daemon.root).find_request("no-such-request") is None
    with open(ours["prompt_path"], "w") as stream:          # the payload digest moves (C-6.2)
        stream.write('{"scenario": "ok", "delay_s": 1}')
    refused = daemon.request("submit", **ours)
    assert not refused["ok"] and refused["error"]["code"] == 2
    assert f"different payload by job {job_id}" in refused["error"]["message"]


def test_c16_3_a_kill_whose_answer_is_lost_reports_what_the_job_became(daemon, monkeypatch, capsys):
    """C-7.1, C-7.2, C-16.3 the first kill cancelled a queued job; its re-send says so, with the rc."""
    daemon.start()
    # No Claude lane in the harness: an Opus job is never launched, so the first
    # kill cancels it in its own transaction and the re-send finds it finished.
    job_id = daemon.submit(pinned_model="opus")
    assert daemon.job(job_id)["state"] in ("queued", "waiting")
    monkeypatch.setenv("SUBFLEET_HOME", str(daemon.root))
    dropped = drop_first_answer(monkeypatch)
    assert cli.main(["kill", job_id]) == 0
    captured = capsys.readouterr()
    assert json.loads(dropped[0])["result"]["status"] == "cancel requested"
    assert captured.out.strip() == f"{job_id} already finished (cancelled, rc 130)"
    assert "acknowledged on re-query" in captured.err
    job = daemon.job(job_id)
    assert job["state"] == "cancelled" and job["rc"] == 130 and job["cancel_requested_at"]
