"""Exhaustive properties of settling a lost answer (C-16.3) and of the notice header (C-15.1).

The example tests in `test_outcome_unknown.py` and `test_notice_rendering.py`
name each path. These enumerate every input in a small finite domain, so each
invariant holds for all of it, not for the cases someone thought of.

Settling: a model daemon holds jobs by request id and applies C-6.2 (same
digest answers the job, another digest is refused, naming the job). Each of
the two sends is refused at connect, lost before the daemon reads it, lost
after the daemon committed it, or answered; the checkout's HEAD may move
between them, which changes the digest. For every combination:

* S1 at most one job carries the request id, and at most two submits are sent;
* S2 a returned answer names that job, and it exists;
* S3 with a request id the CLI minted, nothing but `OutcomeUnknown` is raised
  while a job exists: a submission that committed is never reported refused,
  unavailable, or not done;
* S4 `OutcomeUnknown` is raised only when neither send was answered (or the
  refused re-send's lookup could not be made).

Header: `render.notice_header` against the independent parser of the C-15.1
check the harnesses run (`tests/fake/notice_invariant.py`), a differential over
every state, rc, accepted attempt and `-o` path.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from subfleet import render
from subfleet.client import (Client, DaemonError, DaemonUnavailable, OutcomeUnknown,
                             ResponseLost)
from tests.fake.notice_invariant import notice_mismatches

SEND_OUTCOMES = ("refused-connect", "lost-before", "lost-after", "answered")
REQUEST_ID = "rid-1"


class ModelDaemon:
    """C-6.2 over one request id, with scripted delivery for each call."""

    def __init__(self, sends: tuple[str, str], moved: bool, lookup_lost: bool):
        self.jobs: dict[str, dict] = {}             # request id -> {"job_id", "digest"}
        self.sends = list(sends)
        self.moved = moved
        self.lookup_lost = lookup_lost
        self.submits = 0
        self.answered = 0

    def call(self, op, args=None, *, request_id="", timeout=None):
        if op == "list":
            if self.lookup_lost:
                raise ResponseLost("lookup lost", op=op, request_id=request_id)
            wanted = (args or {}).get("request_id")
            job = self.jobs.get(wanted)
            return {"jobs": [{"job_id": job["job_id"], "request_id": wanted, "state": "queued"}]
                    if job else []}
        assert op == "submit" and request_id == REQUEST_ID
        how = self.sends[self.submits]
        digest = "moved" if self.moved and self.submits == 1 else "first"
        self.submits += 1
        if how == "refused-connect":
            raise DaemonUnavailable("connection refused")
        if how == "lost-before":
            raise ResponseLost("no response", op=op, request_id=request_id)
        existing = self.jobs.get(request_id)
        if existing and existing["digest"] != digest:
            outcome = DaemonError(2, "request id already used with a different payload "
                                     f"by job {existing['job_id']}")
        elif existing:
            outcome = {"job_id": existing["job_id"], "request_id": request_id, "created": False}
        else:
            self.jobs[request_id] = {"job_id": "job-1", "digest": digest}
            outcome = {"job_id": "job-1", "request_id": request_id, "created": True}
        if how == "lost-after":
            raise ResponseLost("no response", op=op, request_id=request_id)
        self.answered += 1
        if isinstance(outcome, DaemonError):
            raise outcome
        return outcome


CASES = list(itertools.product(SEND_OUTCOMES, SEND_OUTCOMES, (False, True), (False, True), (False, True)))


@pytest.mark.parametrize("first,second,moved,minted,lookup_lost", CASES)
def test_c16_3_settling_never_reports_a_committed_submission_as_not_done(
        tmp_path, first, second, moved, minted, lookup_lost):
    """C-16.3, C-6.2: S1 to S4 over every delivery of the submission and its re-send."""
    model = ModelDaemon((first, second), moved, lookup_lost)
    client = Client(tmp_path)
    client.call = model.call                                   # the wire, replaced by the model
    try:
        result = client.call_settled("submit", {"request_id": REQUEST_ID},
                                     request_id=REQUEST_ID, minted=minted)
        raised = None
    except (OutcomeUnknown, DaemonUnavailable, DaemonError, ResponseLost) as exc:
        result, raised = None, exc

    # S1
    assert len(model.jobs) <= 1
    assert model.submits <= 2
    job = model.jobs.get(REQUEST_ID)
    if result is not None:
        # S2
        assert job is not None and result["job_id"] == job["job_id"]
    if minted and job is not None:
        # S3
        assert raised is None or isinstance(raised, OutcomeUnknown), raised
    if isinstance(raised, OutcomeUnknown):
        # S4
        assert model.answered == 0 or lookup_lost
    if raised is not None and not isinstance(raised, OutcomeUnknown) and job is None:
        # A "not done" answer is true: nothing carries the request id.
        assert isinstance(raised, (DaemonUnavailable, DaemonError))


STATES = ("queued", "running", "waiting", "succeeded", "failed", "cancelled", "lost")
RCS = (None, 0, 1, 2, 4, 7, 125, 130)
ACCEPTED = (None, "20260924-074650-fv2-s2/a1", "20260924-074650-fv2-s2/a2")
OUTS = (None, "/work/out.md")


def test_c15_1_the_header_and_the_independent_check_agree_on_every_row(tmp_path):
    """C-15.1: `render.notice_header` never disagrees with the harness check, for any row.

    Only terminal rows get a notice (C-15.1 refuses the rest), so the
    differential runs over the terminal states; for every row it also checks
    that the paths are named exactly when the job was accepted.
    """
    root = Path(tmp_path).resolve()
    checked = 0
    for state, rc, accepted, out in itertools.product(STATES, RCS, ACCEPTED, OUTS):
        job = {"job_id": "20260924-074650-fv2-s2", "state": state, "rc": rc,
               "accepted_attempt_id": accepted, "out_path": out}
        header = render.notice_header(job, root)
        assert header.startswith(f"{job['job_id']}: {state}; rc={'-' if rc is None else rc}; ")
        if state == "succeeded" and accepted is not None:
            seq = accepted.rpartition("/a")[2]
            assert header.endswith(f"; deliverable={root}/jobs/{job['job_id']}/a{seq}/deliverable.md"
                                   f"; out={out or '-'}"), header
        else:
            assert header.endswith("; deliverable=-; out=-"), header
        if state not in ("succeeded", "failed", "cancelled", "lost"):
            continue
        notices = [{"notice_id": 1, "job_id": job["job_id"], "text": header + "\nsummary"}]

        def rows(sql, params=()):
            return notices if sql.lstrip().upper().startswith("SELECT NOTICE_ID") else [job]

        assert notice_mismatches(rows) == [], (job, header)
        checked += 1
    assert checked == 4 * len(RCS) * len(ACCEPTED) * len(OUTS)
