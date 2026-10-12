"""C-16.3, C-16.7: the settle properties with a busy daemon (exhaustive).

S1-S4 of `test_settle_properties`, over every pair of deliveries with a fifth,
`busy` (answered 69 before the request is read), plus S5: exit 69 (busy or a
refused connect) is raised only when no send was read. Written by the final
reviewer of #43 (client lens), who found it fails on the pre-fix client.

A sixth delivery, `lost-inflight`, is read and committed only after the client
looked the request id up (the first submit still queued behind others), so the
lookup finds nothing yet.
"""

from __future__ import annotations

import itertools

import pytest

from subfleet.client import Client, DaemonError, DaemonUnavailable, OutcomeUnknown, ResponseLost
from tests.unit.test_settle_properties import REQUEST_ID, ModelDaemon

OUTCOMES = ("refused-connect", "lost-before", "lost-after", "answered", "busy", "lost-inflight")


class BusyModel(ModelDaemon):
    def __init__(self, sends, moved, lookup_lost):
        super().__init__(sends, moved, lookup_lost)
        self.read = 0
        self.inflight = False
        self.lookups = 0

    def call(self, op, args=None, *, request_id="", timeout=None):
        if op == "list":
            self.lookups += 1
            result = super().call(op, args, request_id=request_id, timeout=timeout)
            if self.inflight:                  # it commits after the lookup
                self.jobs.setdefault(REQUEST_ID, {"job_id": "job-1", "digest": "first"})
                self.inflight = False
            return result
        how = self.sends[self.submits]
        if how == "busy":
            self.submits += 1
            raise DaemonError(69, "the daemon is busy: it holds 512 client connections, its limit",
                              "try again shortly")
        if how == "lost-inflight":
            self.submits += 1
            self.read += 1
            if REQUEST_ID not in self.jobs:
                self.inflight = True
            raise ResponseLost("no response", op=op, request_id=request_id)
        if how != "refused-connect":
            self.read += 1
        return super().call(op, args, request_id=request_id, timeout=timeout)


CASES = list(itertools.product(OUTCOMES, OUTCOMES, (False, True), (False, True), (False, True)))


@pytest.mark.parametrize("first,second,moved,minted,lookup_lost", CASES)
def test_settle_with_busy(tmp_path, first, second, moved, minted, lookup_lost):
    model = BusyModel((first, second), moved, lookup_lost)
    client = Client(tmp_path)
    client.call = model.call
    try:
        result = client.call_settled("submit", {"request_id": REQUEST_ID},
                                     request_id=REQUEST_ID, minted=minted)
        raised = None
    except (OutcomeUnknown, DaemonUnavailable, DaemonError, ResponseLost) as exc:
        result, raised = None, exc
    if model.inflight:                          # never looked up: it commits now
        model.jobs.setdefault(REQUEST_ID, {"job_id": "job-1", "digest": "first"})
    job = model.jobs.get(REQUEST_ID)
    # S1
    assert len(model.jobs) <= 1 and model.submits <= 2
    # S2
    if result is not None:
        assert job is not None and result["job_id"] == job["job_id"]
    # S3: a committed minted submission is never reported refused, busy or unavailable
    if minted and job is not None:
        assert raised is None or isinstance(raised, OutcomeUnknown), raised
    # S4
    if isinstance(raised, OutcomeUnknown):
        assert model.answered == 0 or lookup_lost
    # S5: exit 69 only when nothing was read
    if isinstance(raised, DaemonUnavailable) or (isinstance(raised, DaemonError)
                                                 and not isinstance(raised, OutcomeUnknown)
                                                 and raised.code == 69):
        assert model.read == 0, (first, second, raised)
    # A refusal (not 69) with no job: true only if nothing read is still in flight
    if isinstance(raised, DaemonError) and not isinstance(raised, OutcomeUnknown) and raised.code != 69:
        assert job is None or not minted, (first, second, raised, job)
